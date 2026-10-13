"""Run the 12 unchanged SWE-Marathon gates on the reference and durable adapter.

Uses isolated local Docker containers. The adapter's API and worker are
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

from sregym.service.apps.stripe.application import STRIPE_IMAGE

ROOT = Path(__file__).resolve().parents[4]
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

# The upstream renewal test sleeps exactly as long as Uvicorn's idle timeout.
# Avoid racing its connection close without changing tests or adding retries.
TRANSPORT_PLUGIN = """import pytest
import requests
import stripe


@pytest.fixture(scope="session", autouse=True)
def contract_transport():
    previous_client = stripe.default_http_client
    with requests.Session() as session:
        session.headers["Connection"] = "close"
        stripe.default_http_client = stripe.RequestsClient(session=session)
        try:
            yield
        finally:
            stripe.default_http_client = previous_client
"""


def command(*args, **kwargs):
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.run(["docker", *args], check=True, stdout=subprocess.PIPE, text=True, **kwargs).stdout


def validate(output, image=STRIPE_IMAGE):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = "stripe-contract-" + secrets.token_hex(4)
    test_image = name + "-tests"
    result = {"application_image": image, "passed": False, "modes": {}}
    created = []
    with tempfile.TemporaryDirectory(prefix="stripe-contract-build-") as temporary:
        context = Path(temporary)
        shutil.copytree(ROOT / "docker/apps/stripe/backend/vendor/tests", context / "tests")
        (context / "contract_transport.py").write_text(TRANSPORT_PLUGIN)
        (context / "Dockerfile").write_text(
            f"FROM {image}\nRUN pip install --no-cache-dir stripe==10.10.0 pytest==8.4.1\n"
            "COPY tests/ /tests/\nCOPY contract_transport.py /test-support/\n"
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
                raise RuntimeError(
                    f"{mode} API did not become ready: " + command("logs", api, stderr=subprocess.STDOUT)[-3000:]
                )
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
                        "-e",
                        "PYTHONPATH=/test-support",
                        runner,
                        "python",
                        "-m",
                        "pytest",
                        "-p",
                        "contract_transport",
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
            (mode_out / "api.log").write_text(command("logs", api, stderr=subprocess.STDOUT))
            if mode == "adapter":
                (mode_out / "worker.log").write_text(command("logs", runner, stderr=subprocess.STDOUT))
            command("stop", api)
            if mode == "adapter":
                command("stop", runner)
        result["passed"] = all(g["passed"] for gates in result["modes"].values() for g in gates.values())
    finally:
        for container in reversed(created):
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)
        subprocess.run(["docker", "image", "rm", test_image], capture_output=True)
        (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default=STRIPE_IMAGE, help="Application image to verify")
    args = parser.parse_args()
    result = validate(args.output, args.image)
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
