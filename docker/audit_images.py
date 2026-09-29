"""Build a fresh SREGym runtime-image inventory, then check both Linux platforms.

Run from the repository root with `uv run python docker/audit_images.py`. Requires
Docker/buildx, Helm, Git, curl, and initialized application submodules. Nothing
is deployed: the TrainTicket installer is copied from a stopped container.
Use --output to retain image provenance and failures, or --inventory-only to
inspect references without querying their registry manifests. No workflow is
required. Build-only AMD64 preservation stages and vendored upstream examples
outside SREGym's deployment paths are not runnable SREGym image references.
"""

import argparse
import ast
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from check_image_platforms import check_image, configuration_images, container_images, runtime_images

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "SREGym-applications"
VALUES = ROOT / "sregym/service/apps/values"
EXCLUDED = {
    "app-image:latest": "Deliberately nonexistent image used to inject a pull failure.",
    "pingcap/tidb-operatorr:v1.6.3": "Deliberate spelling error used to inject a pull failure.",
    "sregym-agent-base:latest": "Local development build; published DEFAULT_AGENT_IMAGE is checked.",
    "sregym:latest": "MCP source placeholder; the rendered kustomization is checked.",
}
REFERENCE = re.compile(r"(?:[\w.-]+(?::[0-9]+)?/)*[a-z0-9][a-z0-9._-]*(?::[\w.-]+)?(?:@sha256:[a-f0-9]{64})?")
SOURCE_ROOTS = ("sregym", "scripts", "kind", "mcp_server", "clients")
MANIFEST_ROOTS = (
    "hotelReservation/kubernetes",
    "BlueprintHotelReservation/kubernetes",
    "BlueprintHotelReservation/wlgen",
    "FleetCast/tidb-operator",
)


def run(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True, timeout=180).stdout


def source_images(text: str, python: bool = False) -> set[str]:
    """Find literal image arguments/constants without importing application code."""
    images = set()
    for pattern in (r"(?m)^\s*(?:-\s*)?image:\s*[\"']?([^\s\"']+)", r"--image(?:=|\s+)[\"']?([^\s\"']+)"):
        images.update(re.findall(pattern, text))
    if python:
        for node in ast.walk(ast.parse(text)):
            value = None
            if isinstance(node, ast.keyword) and node.arg and "image" in node.arg.lower():
                value = node.value
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any("image" in ast.unparse(target).lower() for target in targets):
                    value = node.value
            elif isinstance(node, ast.Dict):
                for key, item in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "image" and isinstance(item, ast.Constant):
                        images.add(item.value)
            if isinstance(value, ast.Constant):
                images.add(value.value)
    # A regex over Python annotations can capture `image: str`; actual image
    # arguments here are tagged or qualified, and must match reference syntax.
    return {ref for ref in images if isinstance(ref, str) and REFERENCE.fullmatch(ref) and (":" in ref or "/" in ref)}


class Inventory:
    def __init__(self):
        self.sources: dict[str, set[str]] = {}

    def add(self, images, source):
        for image in images:
            if not REFERENCE.fullmatch(image):
                raise ValueError(f"Unresolved image {image!r} in {source}")
            self.sources.setdefault(image, set()).add(str(source))

    def yaml(self, text, source, configuration=False):
        extract = configuration_images if configuration else container_images
        for doc in yaml.safe_load_all(text):
            self.add(extract(doc), source)

    def chart(self, name, chart, *args):
        self.yaml(run("helm", "template", name, str(chart), *map(str, args)), f"chart:{name}")


def collect_sources(inventory):
    for rel in run("git", "ls-files", "--cached", "--others", "--exclude-standard").splitlines():
        path = ROOT / rel
        if path.parts[len(ROOT.parts)] not in SOURCE_ROOTS or path.suffix not in {
            ".py",
            ".sh",
            ".yaml",
            ".yml",
            ".json",
        }:
            continue
        # Vendored observer charts are rendered below, not parsed as raw YAML.
        if rel.startswith(("sregym/observer/prometheus/", "sregym/observer/filebeat/", "sregym/observer/logstash/")):
            continue
        text = path.read_text()
        if path.suffix in {".yaml", ".yml", ".json"}:
            inventory.yaml(text, rel, configuration=True)
        else:
            inventory.add(source_images(text, python=path.suffix == ".py"), rel)
    for rel in MANIFEST_ROOTS:
        if not (APPS / rel).is_dir():
            raise FileNotFoundError(f"Initialize the application submodules: {APPS / rel}")
        for path in sorted((APPS / rel).rglob("*")):
            if path.suffix not in {".yaml", ".yml"}:
                continue
            if "templates" not in path.parts and path.name not in {"Chart.yaml", "values.yaml"}:
                inventory.yaml(path.read_text(), path.relative_to(ROOT), configuration=True)
    # Include optional operator images, even when create=false.
    operator = APPS / "FleetCast/tidb-operator/values.yaml"
    inventory.yaml(operator.read_text(), operator.relative_to(ROOT), configuration=True)


def collect_charts(inventory):
    for name, chart, values in (
        ("social-network", "socialNetwork/helm-chart/socialnetwork", None),
        ("flight-ticket", "flight-ticket", "flight-ticket-images.yaml"),
        ("train-ticket", "train-ticket", "train-ticket-images.yaml"),
        ("fleetcast", "FleetCast/satellite-app", "fleetcast-images.yaml"),
        ("tidb-operator", "FleetCast/tidb-operator", None),
    ):
        inventory.chart(name, APPS / chart, *(["-f", VALUES / values] if values else []))
    for profile in ("full", "svelte"):
        args = [
            "-f",
            VALUES / "astronomy-shop-fixes.yaml",
            "-f",
            VALUES / "astronomy-shop-arm64.yaml",
            "--set",
            "prometheus.enabled=false",
        ]
        if profile == "svelte":
            args += ["-f", VALUES / "astronomy-shop-svelte.yaml"]
        inventory.chart("astronomy-" + profile, APPS / "astronomy-shop/charts/opentelemetry-demo", *args)
    for name in ("filebeat", "logstash", "prometheus/prometheus"):
        inventory.chart(name.split("/")[0], ROOT / "sregym/observer" / name)
    # Locally cached dependency archives can override the unpacked vendored
    # charts. Cover a clean checkout's images as well as the current render.
    for chart in sorted((ROOT / "sregym/observer/prometheus/prometheus/charts").iterdir()):
        if chart.is_dir() and (chart / "Chart.yaml").is_file():
            inventory.chart("vendored-" + chart.name, chart)
    inventory.chart(
        "prometheus-svelte",
        ROOT / "sregym/observer/prometheus/prometheus",
        "-f",
        ROOT / "sregym/observer/prometheus/values-svelte.yaml",
    )
    for name in ("filebeat", "logstash"):
        inventory.chart(
            name + "-oss",
            ROOT / "sregym/observer" / name,
            "-f",
            ROOT / f"sregym/observer/{name}/examples/oss/values.yaml",
        )
    for name in ("loki", "promtail"):
        inventory.chart(
            name,
            name,
            "--repo",
            "https://grafana.github.io/helm-charts",
            "-f",
            ROOT / f"sregym/observer/loki/{name}-values.yaml",
        )
    inventory.chart("ingress-nginx", "ingress-nginx", "--repo", "https://kubernetes.github.io/ingress-nginx")
    inventory.chart(
        "chaos-mesh",
        "chaos-mesh",
        "--repo",
        "https://charts.chaos-mesh.org",
        "--version",
        "2.8.0",
        "-f",
        ROOT / "sregym/generators/noise/impl/chaos-mesh-values.yaml",
    )
    inventory.yaml(run("kubectl", "kustomize", str(ROOT / "mcp_server/k8s")), "rendered:mcp")


def checkout(directory, repository, revision):
    directory.mkdir()
    run("git", "init", "--quiet", cwd=directory)
    run("git", "fetch", "--quiet", "--depth=1", repository, revision, cwd=directory)
    run("git", "checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=directory)


def installer_manifests(script: str) -> set[str]:
    """Resolve TrainTicket's literal apply paths and cp-generated manifests.

    Its deploy.yaml is overwritten from deploy.yaml.sample before apply, and
    the SkyWalking apply commands are commented out. Inspect the actual script
    rather than treating every old file shipped in the image as active.
    """
    variables = dict(re.findall(r'^([\w]+)=["\']?([^\s"\']+)["\']?$', script, re.MULTILINE))

    def resolve(value):
        result = re.sub(r"\$\{?(\w+)\}?", lambda match: variables.get(match[1], match[0]), value)
        if "$" in result or not result.startswith("deployment/") or ".." in Path(result).parts:
            raise ValueError(f"Unresolved installer manifest path: {value}")
        return result

    copies, applied = {}, set()
    for line in script.splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] == ["cp"] and len(words) == 3:
            copies[resolve(words[2])] = resolve(words[1])
        if words[:3] == ["kubectl", "apply", "-f"]:
            applied.add(resolve(words[3]))
    if not applied:
        raise ValueError("No manifest applications found in the TrainTicket installer")
    return {copies.get(path, path) for path in applied}


def collect_external(inventory, temporary, images):
    # Follow the setup script's pinned chart revision, without deploying a release.
    setup = (APPS / "flight-ticket/setup_openwhisk.sh").read_text()
    revision = re.search(r"^chart_revision=([a-f0-9]{40})$", setup, re.MULTILINE)
    if not revision:
        raise ValueError("Cannot resolve the OpenWhisk setup chart revision")
    chart = temporary / "openwhisk"
    checkout(chart, "https://github.com/apache/openwhisk-deploy-kube.git", revision[1])
    run("git", "apply", str(APPS / "flight-ticket/openwhisk/catalog.patch"), cwd=chart)
    shutil.copyfile(APPS / "flight-ticket/openwhisk/runtimes.json", chart / "helm/openwhisk/sregym-runtimes.json")
    inventory.chart("openwhisk", chart / "helm/openwhisk", "-f", APPS / "flight-ticket/openwhisk/values.yaml")
    inventory.add(
        runtime_images(json.loads((APPS / "flight-ticket/openwhisk/runtimes.json").read_text())),
        "OpenWhisk runtime catalog",
    )

    # Ansible uses a separate operator release; don't substitute FleetCast's chart.
    chart = temporary / "tidb-operator"
    checkout(chart, "https://github.com/pingcap/tidb-operator.git", "v1.6.0")
    inventory.chart(
        "tidb-ansible",
        chart / "charts/tidb-operator",
        "-f",
        ROOT / "scripts/ansible/tidb/tidb-operator.yaml",
        "--set",
        "advancedStatefulset.create=true",
        "--set",
        "scheduler.create=true",
    )

    for url in (
        "https://openebs.github.io/charts/openebs-operator.yaml",
        "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
        "https://raw.githubusercontent.com/projectcalico/calico/v3.27.0/manifests/calico.yaml",
        "https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml",
    ):
        inventory.yaml(run("curl", "-fsSL", "--max-time", "120", url), url)

    # The deployed TrainTicket image embeds manifests and charts. Reading only
    # the submodule's source would miss what the installer actually launches.
    container = run("docker", "create", images["train-ticket-deploy"]).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise ValueError(f"Unexpected stopped container identifier: {container!r}")
    try:
        run("docker", "cp", f"{container}:/usr/local/bin/deployment", str(temporary / "deployment"))
        run("docker", "cp", f"{container}:/usr/local/bin/deploy.sh", str(temporary / "deploy.sh"))
    finally:
        run("docker", "rm", container)
    manifests = temporary / "deployment/kubernetes-manifests"
    for rel in sorted(installer_manifests((temporary / "deploy.sh").read_text())):
        target = temporary / rel
        files = sorted(target.iterdir()) if target.is_dir() else [target]
        for path in files:
            if path.name.endswith((".yaml", ".yml", ".yaml.sample")):
                inventory.yaml(path.read_text(), f"TrainTicket installer:{path.relative_to(manifests)}")
    for name in ("mysql", "nacos", "rabbitmq"):
        inventory.chart("train-" + name, manifests / "quickstart-k8s/charts" / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Write resolved image references, sources, exclusions, and results as JSON"
    )
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    inventory = Inventory()
    images = json.loads((ROOT / "docker/images.lock.json").read_text())
    inventory.add(images.values(), "docker/images.lock.json")
    collect_sources(inventory)
    collect_charts(inventory)
    with tempfile.TemporaryDirectory(prefix="sregym-image-audit-") as temporary:
        collect_external(inventory, Path(temporary), images)
    exclusions = {
        ref: {"reason": reason, "sources": sorted(inventory.sources.pop(ref))}
        for ref, reason in EXCLUDED.items()
        if ref in inventory.sources
    }
    references = sorted(inventory.sources)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = (
            list(executor.map(check_image, references))
            if not args.inventory_only
            else [(ref, None) for ref in references]
        )
    records = [{"image": ref, "sources": sorted(inventory.sources[ref]), "error": error} for ref, error in results]
    if args.output:
        args.output.write_text(
            json.dumps(
                {"images": records, "exclusions": exclusions, "registry_checked": not args.inventory_only}, indent=2
            )
            + "\n"
        )
    for ref, error in results:
        print(("INVENTORY" if args.inventory_only else "FAIL" if error else "PASS"), ref, error or "")
    print(
        f"{len(results)} references; {sum(error is not None for _, error in results)} failures; {len(exclusions)} explicit exclusions"
    )
    return int(any(error is not None for _, error in results))


if __name__ == "__main__":
    raise SystemExit(main())
