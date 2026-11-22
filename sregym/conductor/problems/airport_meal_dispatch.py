"""Deploy the live airport meal dispatch and issuance workers."""

import base64
import hashlib
import json
import os
import shlex
from pathlib import Path

ASSETS = Path(__file__).with_name("assets") / "airport_meal_clearing" / "dispatch"
RUNTIME = "meal-bridge-runtime"
IMAGE = "apache/kafka:3.7.2"


def _deployment(name, role, jar_digest):
    labels = {"app": name}
    env = [{"name": "KAFKA_BOOTSTRAP_SERVERS", "value": "kafka:9092"}]
    if role == "issuer":
        env.append({"name": "ISSUANCE_MODE", "value": "legacy"})
    mount = "/opt/meal-bridge"
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels, "annotations": {"meal-bridge-jar": jar_digest}},
                "spec": {
                    "terminationGracePeriodSeconds": 60,
                    "containers": [
                        {
                            "name": role,
                            "image": IMAGE,
                            "command": [
                                "java",
                                "-Xmx128m",
                                "-XX:ActiveProcessorCount=2",
                                "-Dlog4j.configuration=file:" + mount + "/log4j.properties",
                                "-cp",
                                mount + "/meal-dispatch.jar:/opt/kafka/libs/*",
                                "airport.meal.MealBridge",
                                role,
                            ],
                            "env": env,
                            "readinessProbe": {
                                "exec": {"command": ["test", "-f", "/tmp/meal-bridge-ready"]},
                                "periodSeconds": 2,
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "96Mi"},
                                "limits": {"memory": "256Mi"},
                            },
                            "volumeMounts": [{"name": "runtime", "mountPath": mount, "readOnly": True}],
                        }
                    ],
                    "volumes": [{"name": "runtime", "configMap": {"name": RUNTIME}}],
                },
            },
        },
    }


def deploy(problem):
    """Install the live bridge after the history has finished and Kafka is ready."""
    artifact = Path(os.getenv("AIRPORT_MEAL_DISPATCH_ARTIFACTS", str(ASSETS / "dist"))) / "meal-dispatch.jar"
    if not artifact.is_file():
        raise FileNotFoundError(
            f"Missing {artifact}. Run dispatch/build.sh on the build host and set AIRPORT_MEAL_DISPATCH_ARTIFACTS."
        )
    jar = artifact.read_bytes()
    digest = hashlib.sha256(jar).hexdigest()
    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": RUNTIME},
        "binaryData": {"meal-dispatch.jar": base64.b64encode(jar).decode("ascii")},
        "data": {
            "INTERFACES.md": (ASSETS / "INTERFACES.md").read_text(encoding="utf-8"),
            "log4j.properties": (ASSETS / "log4j.properties").read_text(encoding="utf-8"),
        },
    }

    def kubectl(*args, input_data=None, timeout=130):
        problem.kubectl.exec_command_checked(
            shlex.join(["kubectl", "-n", problem.namespace, *args]),
            input_data=input_data,
            timeout=timeout,
        )

    kubectl(
        "apply",
        "-f",
        "-",
        input_data=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    config,
                    _deployment("meal-dispatch", "dispatch", digest),
                    _deployment("meal-issuer", "issuer", digest),
                ],
            }
        ),
    )
    for workload in ("meal-dispatch", "meal-issuer"):
        kubectl("rollout", "status", f"deployment/{workload}", "--timeout=120s")
