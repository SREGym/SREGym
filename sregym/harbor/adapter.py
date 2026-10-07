"""Generate Harbor tasks from SREGym problems.

Usage:
    uv run python -m sregym.harbor.adapter --output-dir datasets/sregym
    uv run python -m sregym.harbor.adapter --suite sregym-lite --output-dir datasets/sregym-lite
    uv run python -m sregym.harbor.adapter --task-ids network_policy_block incorrect_image

Each generated task is one unprivileged container built on the SREGym Harbor
image (``docker/harbor``): a four-node k3s cluster, the backend that deploys the
problem and grades it with the problem's mitigation oracle, and the agent, which
runs as an unprivileged user. See ``sregym/harbor/backend.py`` and
``docs/harbor.md``.

Problem metadata is read by constructing each problem against a placeholder
kubeconfig, so no cluster is needed. Problems that cannot run in the per-task
KIND cluster, or cannot be graded, are skipped with the reason reported.
"""

import argparse
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sregym.harbor import protocol, selftest

TEMPLATE_DIR = Path(__file__).parent / "task-template"
DATASET_README = Path(__file__).parent / "dataset-readme.md"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKEND_IMAGE = "ghcr.io/sregym/sregym-harbor:latest"
# SREGym's own runner gives agents 1800s per attempt.
DEFAULT_AGENT_TIMEOUT_S = 1800
# Default oracle budget: the base mitigation oracle waits up to 60s for
# rollouts, and custom oracles can declare a longer evaluation_timeout_seconds.
DEFAULT_GRADE_TIMEOUT_S = 900
READY_WAIT_S = 300
READY_BUDGET_S = 3600

_OFFLINE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters: [{name: offline, cluster: {server: "https://127.0.0.1:9"}}]
contexts: [{name: offline, context: {cluster: offline, user: offline}}]
current-context: offline
users: [{name: offline, user: {token: offline}}]
"""


# Problems the unprivileged k3s cluster cannot run, with the reason. Validate a
# problem with docker/userns-k3s/validate-sregym.sh before removing it here.
K3S_UNSUPPORTED = {
    "kubelet_crash": "injects its fault with docker exec into KIND node containers",
    "kubelet_eviction_threshold_misconfig": "injects its fault with docker exec into KIND node containers",
    "calico_route_reflector_label_drift_hotel_reservation": "needs Calico; the cluster runs flannel",
    "pod_cidr_exhaustion_hotel_reservation": "needs Calico; the cluster runs flannel",
    "node_conntrack_exhaustion_hotel_reservation": "writes a host-global sysctl",
    "workload_imbalance": "replaces the kube-proxy DaemonSet; k3s has kube-proxy built in",
}


@dataclass
class ProblemInfo:
    problem_id: str
    app_name: str
    app_description: str
    namespaces: list[str]
    grade_timeout_s: int = DEFAULT_GRADE_TIMEOUT_S


SELFTEST = ProblemInfo(
    problem_id=selftest.PROBLEM_ID,
    app_name=selftest.APP_NAME,
    app_description=selftest.APP_DESCRIPTION,
    namespaces=[selftest.NAMESPACE],
    grade_timeout_s=selftest.GRADE_TIMEOUT_S + 60,
)


@dataclass
class Inspection:
    eligible: list[ProblemInfo] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


DEFAULT_DATASET_NAME = "sregym/sregym"
SELFTEST_DATASET_NAME = "sregym/harbor-selftest"
# Title and summary shown on Harbor Hub for the datasets SREGym publishes.
_DATASET_TEXT = {
    "sregym/sregym": (
        "SREGym",
        "This dataset holds every SREGym problem that runs on an emulated (KIND) "
        "cluster: {count} tasks across microservice applications, fault types and "
        "difficulty levels.",
    ),
    "sregym/sregym-lite": (
        "SREGym-Lite",
        "SREGym-Lite is a curated set of {count} problems with varied difficulty and "
        "failure mechanisms, chosen to run reliably. It is the recommended starting point.",
    ),
    SELFTEST_DATASET_NAME: (
        "SREGym Harbor self-test",
        "A single small task that checks whether a Harbor environment can run SREGym: "
        "its unprivileged cluster, the API proxy, grading and the "
        "reference solution. It takes a few minutes.",
    ),
}


def task_name(problem_id: str) -> str:
    """Stable Harbor task/directory name for a problem ID."""
    return "".join(c if c.isalnum() else "-" for c in problem_id.lower()).strip("-")


def _inspect_in_this_process(problem_ids: list[str] | None) -> Inspection:
    """Inspect problems. Must run with KUBECONFIG set before kubernetes is imported."""
    from sregym.conductor.problems.registry import ProblemRegistry

    inspection = Inspection()
    registry = ProblemRegistry()
    all_ids = registry.get_problem_ids(all=True)
    unknown = sorted(set(problem_ids or ()) - set(all_ids))
    if unknown:
        raise ValueError(f"Unknown SREGym problem IDs: {', '.join(unknown)}")
    for problem_id in problem_ids or all_ids:
        if problem_id in registry.non_emulated_cluster_problems:
            inspection.skipped[problem_id] = "requires a non-emulated cluster"
            continue
        if problem_id in K3S_UNSUPPORTED:
            inspection.skipped[problem_id] = K3S_UNSUPPORTED[problem_id]
            continue
        try:
            problem = registry.get_problem(problem_id)()
        except (Exception, SystemExit) as exc:  # KubeCtl exits on API errors
            inspection.skipped[problem_id] = f"cannot be inspected without a cluster ({type(exc).__name__})"
            continue
        oracle = getattr(problem, "mitigation_oracle", None)
        if oracle is None:
            inspection.skipped[problem_id] = "has no mitigation oracle to grade with"
            continue
        app = problem.app
        oracle_timeout = getattr(oracle, "evaluation_timeout_seconds", None) or 0
        inspection.eligible.append(
            ProblemInfo(
                problem_id=problem_id,
                app_name=app.app_name,
                app_description=str(app.description).strip(),
                namespaces=list(getattr(app, "namespaces", None) or [app.namespace]),
                grade_timeout_s=max(DEFAULT_GRADE_TIMEOUT_S, int(oracle_timeout) + 300),
            )
        )
    return inspection


def _inspection_worker(output: str, problem_ids: list[str]) -> None:
    """Child-process entry point for inspect_problems()."""
    # Connection failures against the placeholder cluster are expected, and
    # constructors print progress; keep both out of the generator's output.
    sregym_logger = logging.getLogger("all")
    sregym_logger.addHandler(logging.NullHandler())
    sregym_logger.propagate = False
    try:
        with contextlib.redirect_stdout(sys.stderr):
            inspection = _inspect_in_this_process(problem_ids or None)
        result = {"eligible": [asdict(info) for info in inspection.eligible], "skipped": inspection.skipped}
    except ValueError as exc:
        result = {"error": str(exc)}
    Path(output).write_text(json.dumps(result))


def inspect_problems(problem_ids: list[str] | None = None) -> Inspection:
    """Collect metadata for eligible problems and reasons for the rest.

    Problem constructors build their app and oracle objects without contacting
    the API server, apart from a few that inspect nodes. Inspection runs in a
    child process whose kubeconfig points at a closed local port, so it never
    touches whatever cluster the caller's kubeconfig selects.
    """
    with tempfile.TemporaryDirectory() as tmp:
        kubeconfig = Path(tmp) / "kubeconfig"
        kubeconfig.write_text(_OFFLINE_KUBECONFIG)
        output = Path(tmp) / "inspection.json"
        env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
        command = [
            sys.executable,
            "-c",
            "import sys; from sregym.harbor.adapter import _inspection_worker; "
            "_inspection_worker(sys.argv[1], sys.argv[2:])",
            str(output),
            *(problem_ids or ()),
        ]
        completed = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
        if completed.returncode != 0 or not output.exists():
            raise RuntimeError(f"Problem inspection failed:\n{completed.stderr[-4000:]}")
        result = json.loads(output.read_text())
    if "error" in result:
        raise ValueError(result["error"])
    return Inspection(
        eligible=[ProblemInfo(**info) for info in result["eligible"]],
        skipped=result["skipped"],
    )


def oracle_token(secret: str, name: str) -> str:
    """The token that lets task ``name``'s reference solution trigger recovery."""
    return hmac.new(secret.encode(), name.encode(), hashlib.sha256).hexdigest()


def resolve_oracle_secret(output_dir: Path) -> tuple[str, Path | None]:
    """Return the oracle secret and, if one was generated or reused, its file.

    ``SREGYM_ORACLE_SECRET`` wins. Otherwise a secret saved at the dataset root
    by an earlier run is reused, so tasks generated into the same directory
    share it, or a new one is generated and saved there.
    """
    secret = os.environ.get(protocol.ORACLE_SECRET_ENV, "").strip()
    if secret:
        return secret, None
    path = output_dir / protocol.ORACLE_SECRET_FILE
    if path.exists():
        return path.read_text().strip(), path
    output_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secret + "\n")
    return secret, path


def _render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    # BibTeX uses double braces too; a leftover placeholder is {{lowercase_name}}.
    leftover = re.search(r"\{\{[a-z_]+\}\}", text)
    if leftover:
        raise ValueError(f"Unrendered template placeholder near: {text[leftover.start() :][:40]!r}")
    return text


class SREGymAdapter:
    """Writes one Harbor task directory per eligible SREGym problem."""

    def __init__(
        self,
        output_dir: Path,
        *,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        self_test: bool = False,
        backend_image: str = DEFAULT_BACKEND_IMAGE,
        agent_timeout: int = DEFAULT_AGENT_TIMEOUT_S,
        cpus: int = 8,
        memory_mb: int = 16384,
        storage_mb: int = 51200,
        dataset_name: str = DEFAULT_DATASET_NAME,
    ):
        self.output_dir = output_dir
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.self_test = self_test
        self.backend_image = backend_image
        self.agent_timeout = agent_timeout
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.storage_mb = storage_mb
        self.dataset_name = dataset_name
        self._oracle_secret: str | None = None
        self.oracle_secret_file: Path | None = None

    @property
    def oracle_secret(self) -> str:
        if self._oracle_secret is None:
            self._oracle_secret, self.oracle_secret_file = resolve_oracle_secret(self.output_dir)
        return self._oracle_secret

    def _values(self, info: ProblemInfo) -> dict[str, str]:
        if len(info.namespaces) > 1:
            namespace_block = (
                f"Namespaces: {', '.join(info.namespaces)}\n"
                "(This scenario spans multiple namespaces; investigate all of them.)"
            )
        else:
            namespace_block = f"Namespace: {info.namespaces[0]}"
        app_slug = task_name(info.app_name)
        return {
            "task_name": task_name(info.problem_id),
            "problem_id": info.problem_id,
            "app_name": info.app_name,
            "app_description": info.app_description,
            "namespace_block": namespace_block,
            "keywords": json.dumps(["sregym", "sre", "kubernetes", app_slug]),
            "agent_timeout": f"{float(self.agent_timeout)}",
            "cpus": str(self.cpus),
            "memory_mb": str(self.memory_mb),
            "storage_mb": str(self.storage_mb),
            "dataset_name": self.dataset_name,
            "grade_timeout": str(info.grade_timeout_s),
            "grade_timeout_hook": f"{float(info.grade_timeout_s + 60)}",
            "ready_wait": str(READY_WAIT_S),
            "ready_timeout": f"{float(READY_WAIT_S + 60)}",
            "ready_retries": str(READY_BUDGET_S // READY_WAIT_S),
            "backend_image": self.backend_image,
            "oracle_secret_env": protocol.ORACLE_SECRET_ENV,
            "oracle_token_sha256": hashlib.sha256(
                oracle_token(self.oracle_secret, task_name(info.problem_id)).encode()
            ).hexdigest(),
            "service_name": protocol.SERVICE_NAME,
            "api_port": str(protocol.API_PORT),
            "grade_port": str(protocol.GRADE_PORT),
            "grade_path": protocol.GRADE_PATH,
            "grade_token_path": protocol.GRADE_TOKEN_PATH,
            "log_dir": protocol.LOG_DIR,
            "agent_shared_dir": protocol.AGENT_SHARED_DIR,
            "backend_shared_dir": protocol.BACKEND_SHARED_DIR,
            "kubeconfig_name": protocol.KUBECONFIG_NAME,
            "state_name": protocol.STATE_NAME,
            "status_name": protocol.STATUS_NAME,
            "state_ready": protocol.STATE_READY,
            "state_failed": protocol.STATE_FAILED,
        }

    def generate_task(self, info: ProblemInfo) -> Path:
        task_dir = self.output_dir / task_name(info.problem_id)
        if task_dir.exists():
            if not self.overwrite:
                raise FileExistsError(f"{task_dir} exists; pass --overwrite to replace it")
            shutil.rmtree(task_dir)
        values = self._values(info)
        for source in sorted(TEMPLATE_DIR.rglob("*")):
            if source.is_dir() or "__pycache__" in source.parts:
                continue
            target = task_dir / source.relative_to(TEMPLATE_DIR)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_render(source.read_text(), values))
            if source.suffix == ".sh" or source.name == "sregym-ready":
                target.chmod(0o755)
        return task_dir

    def write_dataset_readme(self) -> Path:
        """Write the dataset README that Harbor Hub shows, listing every task in the directory."""
        rows = []
        for config_path in sorted(self.output_dir.glob("*/task.toml")):
            config = tomllib.loads(config_path.read_text())
            name = config.get("task", {}).get("name", config_path.parent.name)
            rows.append(f"| `{name}` | {config.get('metadata', {}).get('application', '')} |")
        title, summary = _DATASET_TEXT.get(self.dataset_name, (self.dataset_name, "{count} SREGym tasks."))
        readme = self.output_dir / "README.md"
        readme.write_text(
            _render(
                DATASET_README.read_text(),
                {
                    "dataset_title": title,
                    "dataset_summary": summary.format(count=len(rows)),
                    "dataset_name": self.dataset_name,
                    "cpus": str(self.cpus),
                    "memory_mb": str(self.memory_mb),
                    "storage_mb": str(self.storage_mb),
                    "task_rows": "\n".join(rows),
                },
            )
        )
        return readme

    def run(self) -> tuple[list[Path], dict[str, str]]:
        inspection = Inspection(eligible=[SELFTEST]) if self.self_test else inspect_problems(self.task_ids)
        eligible = inspection.eligible[: self.limit] if self.limit is not None else inspection.eligible
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = [self.generate_task(info) for info in eligible]
        if written:
            self.write_dataset_readme()
        return written, inspection.skipped


def main(argv=None) -> int:
    from sregym.conductor.problem_sets import PROBLEM_SETS

    parser = argparse.ArgumentParser(description="Generate Harbor tasks from SREGym problems.")
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/sregym"), help="Where to write tasks")
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N eligible tasks")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing task directories")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--task-ids", nargs="+", default=None, help="SREGym problem IDs to generate")
    selection.add_argument("--suite", choices=sorted(PROBLEM_SETS), help="Generate a named SREGym problem set")
    selection.add_argument(
        "--self-test",
        action="store_true",
        help="Generate only a small task that checks a Harbor environment can run SREGym",
    )
    parser.add_argument(
        "--backend-image",
        default=DEFAULT_BACKEND_IMAGE,
        help="SREGym Harbor image (docker/harbor) the task image is built on",
    )
    parser.add_argument(
        "--dataset-name",
        default=None,
        help="Harbor Hub dataset name used in the README (default: sregym/<suite>, sregym/harbor-selftest "
        f"or {DEFAULT_DATASET_NAME})",
    )
    parser.add_argument("--agent-timeout", type=int, default=DEFAULT_AGENT_TIMEOUT_S, help="Agent time limit (s)")
    parser.add_argument("--cpus", type=int, default=8, help="CPUs requested for the task container")
    parser.add_argument("--memory-mb", type=int, default=16384, help="Memory requested for the task container")
    parser.add_argument("--storage-mb", type=int, default=51200, help="Disk requested for the task container")
    args = parser.parse_args(argv)

    task_ids = list(PROBLEM_SETS[args.suite]) if args.suite else args.task_ids
    if args.dataset_name:
        dataset_name = args.dataset_name
    elif args.self_test:
        dataset_name = SELFTEST_DATASET_NAME
    elif args.suite:
        dataset_name = f"sregym/{args.suite}"
    else:
        dataset_name = DEFAULT_DATASET_NAME
    adapter = SREGymAdapter(
        args.output_dir,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=task_ids,
        self_test=args.self_test,
        backend_image=args.backend_image,
        agent_timeout=args.agent_timeout,
        cpus=args.cpus,
        memory_mb=args.memory_mb,
        storage_mb=args.storage_mb,
        dataset_name=dataset_name,
    )
    try:
        written, skipped = adapter.run()
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    for problem_id, reason in sorted(skipped.items()):
        print(f"skipped {problem_id}: {reason}", file=sys.stderr)
    print(f"Wrote {len(written)} Harbor task(s) to {args.output_dir} ({len(skipped)} skipped)")
    if written and adapter.oracle_secret_file:
        print(
            f"The oracle agent needs {protocol.ORACLE_SECRET_ENV}, saved in {adapter.oracle_secret_file} "
            f"(never published):\n  export {protocol.ORACLE_SECRET_ENV}=$(cat {adapter.oracle_secret_file})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
