"""Run the 12 unchanged SWE-Marathon gates on the reference and durable adapter.

Uses only the private DinD Docker daemon. The adapter's API and worker are
separate containers sharing a network namespace, so upstream localhost webhook
fixtures work unchanged. The normal SREGym deployment uses separate pods.
"""

import argparse
import json
import secrets
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from sregym.service.apps.stripe_marathon import STRIPE_IMAGE

ROOT = Path(__file__).resolve().parents[2]
GATES = (
    "anti_cheat",
    "auth",
    "customers",
    "payment_intents",
    "refunds",
    "idempotency",
    "webhooks",
    "pagination",
    "errors",
    "subscriptions",
    "restricted_keys",
    "concurrency",
)


def command(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kwargs).stdout


def validate(output):
    if not Path("/run/sregym-ready").exists():
        raise RuntimeError("This test requires a private SREGym DinD environment")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = "stripe-contract-" + secrets.token_hex(4)
    test_image = STRIPE_IMAGE + "-tests"
    result = {"application_image": STRIPE_IMAGE, "passed": False, "modes": {}}
    created = []
    with tempfile.TemporaryDirectory(prefix="stripe-contract-build-") as temporary:
        context = Path(temporary)
        shutil.copytree(ROOT / "sregym/service/apps/fixtures/swe-marathon-stripe/tests", context / "tests")
        (context / "Dockerfile").write_text(
            f"FROM {STRIPE_IMAGE}\nRUN pip install --no-cache-dir stripe==10.10.0 pytest==8.4.1\nCOPY tests/ /tests/\n"
        )
        built = subprocess.run(["docker", "build", "-t", test_image, str(context)], capture_output=True, text=True)
        (output / "build.log").write_text(built.stdout + built.stderr)
        built.check_returncode()
    try:
        command("network", "create", name)
        secret = "sk_test_contract_" + secrets.token_hex(16)
        environment = [
            "-e",
            "STRIPE_SK=" + secret,
            "-e",
            "STRIPE_IDEMPOTENCY_TTL=60",
            "-e",
            "STRIPE_WEBHOOK_RETRY_SCHEDULE=1,2,4",
            "-e",
            "STRIPE_BILLING_INTERVAL_SECONDS=2",
        ]
        for mode in ("reference", "adapter"):
            mode_out = output / mode
            mode_out.mkdir(exist_ok=True)
            api = name + "-" + mode
            extra = []
            if mode == "adapter":
                db = name + "-db"
                command(
                    "run",
                    "-d",
                    "--name",
                    db,
                    "--network",
                    name,
                    "--network-alias",
                    "contract-db",
                    "-e",
                    "POSTGRES_PASSWORD=local-contract-only",
                    "postgres:16.14-alpine",
                )
                created.append(db)
                for _ in range(90):
                    ready = subprocess.run(["docker", "exec", db, "pg_isready", "-U", "postgres"], capture_output=True)
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Contract-test PostgreSQL did not become ready")
                extra = ["-e", "DATABASE_URL=postgresql://postgres:local-contract-only@contract-db/postgres"]
            command(
                "run",
                "-d",
                "--name",
                api,
                "--network",
                name,
                "--mount",
                f"type=bind,src={mode_out},dst=/results",
                *environment,
                *extra,
                test_image,
                "python",
                *(["-m", "app"] if mode == "reference" else ["backend.py"]),
            )
            created.append(api)
            for _ in range(60):
                ready = subprocess.run(
                    [
                        "docker",
                        "exec",
                        api,
                        "python",
                        "-c",
                        "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/health', timeout=2).read()",
                    ],
                    capture_output=True,
                )
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"{mode} API did not become ready: " + command("logs", api)[-3000:])
            runner = api
            if mode == "adapter":
                runner = name + "-worker"
                command(
                    "run",
                    "-d",
                    "--name",
                    runner,
                    "--network",
                    "container:" + api,
                    "--mount",
                    f"type=bind,src={mode_out},dst=/results",
                    *environment,
                    *extra,
                    test_image,
                    "python",
                    "backend.py",
                    "worker",
                )
                created.append(runner)
            gates = {}
            for gate in GATES:
                print(f"CONTRACT {mode} {gate}", flush=True)
                proc = subprocess.run(
                    [
                        "docker",
                        "exec",
                        runner,
                        "python",
                        "-m",
                        "pytest",
                        f"/tests/test_{gate}.py",
                        "-q",
                        "--tb=short",
                        f"--junitxml=/results/{gate}.xml",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
                (mode_out / f"{gate}.log").write_text(proc.stdout + proc.stderr)
                report = mode_out / f"{gate}.xml"
                suites = list(ET.parse(report).getroot()) if report.exists() else []
                counts = {
                    key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")
                }
                gates[gate] = {
                    "passed": proc.returncode == 0 and counts["tests"] > 0 and not counts["skipped"],
                    "exit_code": proc.returncode,
                    **counts,
                }
                result["modes"][mode] = gates
                (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
            (mode_out / "api.log").write_text(command("logs", api))
            if mode == "adapter":
                (mode_out / "worker.log").write_text(command("logs", runner))
            command("stop", api)
            if mode == "adapter":
                command("stop", runner)
        result["passed"] = all(g["passed"] for gates in result["modes"].values() for g in gates.values())
    finally:
        for container in reversed(created):
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)
        (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.output)
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
