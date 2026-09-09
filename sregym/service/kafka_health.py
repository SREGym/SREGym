"""Bounded Kafka checks that do not launch another JVM inside the broker."""

import contextlib
import shlex
import time
import uuid

from kubernetes import client


def broker_pods(kubectl, namespace):
    return [
        pod
        for pod in kubectl.list_pods(namespace).items
        if (pod.metadata.labels or {}).get("app.kubernetes.io/name") == "kafka"
        and pod.metadata.deletion_timestamp is None
    ]


def broker_memory_failure(kubectl, namespace, since):
    """Find a new kernel OOM kill or Java heap failure, not an old restart."""
    for pod in broker_pods(kubectl, namespace):
        statuses = [status for status in pod.status.container_statuses or [] if status.name == "kafka"]
        for status in statuses:
            for state in (status.state, status.last_state):
                terminated = state.terminated if state else None
                if (
                    terminated
                    and terminated.reason == "OOMKilled"
                    and terminated.finished_at
                    and terminated.finished_at >= since
                ):
                    return True
            previous = status.last_state.terminated if status.last_state else None
            log_sources = []
            if status.state and status.state.running:
                log_sources.append(False)
            if previous and previous.finished_at and previous.finished_at >= since:
                log_sources.append(True)
            for use_previous in log_sources:
                command = shlex.join(
                    [
                        "kubectl",
                        "logs",
                        "-n",
                        namespace,
                        pod.metadata.name,
                        "-c",
                        "kafka",
                        "--tail=200",
                        "--since-time=" + since.isoformat(),
                    ]
                )
                if use_previous:
                    command += " --previous"
                output = kubectl.exec_command_checked(command, timeout=15)
                if "java.lang.OutOfMemoryError" in output:
                    return True
    return False


class KafkaHealthCheck:
    """Create a temporary client using the broker's already-required image.

    Each check writes a fresh record and reads it back from a temporary topic.
    The client's heap and native memory belong to its own container, not Kafka.
    """

    def __init__(self, kubectl, namespace):
        self.kubectl = kubectl
        self.namespace = namespace
        self.name = "broker-healthcheck-" + uuid.uuid4().hex[:12]

    def __enter__(self):
        deployment = self.kubectl.get_deployment("kafka", self.namespace)
        container = next(c for c in deployment.spec.template.spec.containers if c.name == "kafka")
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=self.name, labels={"app": "broker-healthcheck"}),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                active_deadline_seconds=900,
                containers=[
                    client.V1Container(
                        name="client",
                        image=container.image,
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", "sleep 900"],
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": "25m", "memory": "64Mi"},
                            limits={"memory": "256Mi"},
                        ),
                    )
                ],
            ),
        )
        self.kubectl.core_v1_api.create_namespaced_pod(self.namespace, pod)
        try:
            self.kubectl.exec_command_checked(
                shlex.join(
                    [
                        "kubectl",
                        "wait",
                        "-n",
                        self.namespace,
                        "pod/" + self.name,
                        "--for=condition=Ready",
                        "--timeout=90s",
                    ]
                ),
                timeout=95,
            )
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        # Namespace teardown is a second cleanup boundary if the API is down.
        with contextlib.suppress(Exception):
            self.kubectl.core_v1_api.delete_namespaced_pod(
                self.name,
                self.namespace,
                grace_period_seconds=0,
            )

    def check(self):
        token = uuid.uuid4().hex
        topic = "healthchecks-" + token
        script = f"""export KAFKA_HEAP_OPTS='-Xms16m -Xmx64m' KAFKA_OPTS=''
printf 'request.timeout.ms=3000\ndefault.api.timeout.ms=5000\nretries=0\n' > /tmp/admin.properties
cleanup() {{ /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --command-config /tmp/admin.properties --delete --if-exists --topic {topic} >/dev/null 2>&1; }}
trap cleanup EXIT
if ! /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --command-config /tmp/admin.properties --create --topic {topic} --partitions 1 --replication-factor 1; then
    echo KAFKA_UNAVAILABLE; exit 0
fi
if ! printf '%s\\n' {token} | /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server kafka:9092 --topic {topic} --sync --producer-property max.block.ms=5000 --producer-property request.timeout.ms=3000 --producer-property delivery.timeout.ms=5000; then
    echo KAFKA_UNAVAILABLE; exit 0
fi
if ! /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:9092 --topic {topic} --partition 0 --offset 0 --max-messages 1 --timeout-ms 5000; then
    echo KAFKA_UNAVAILABLE; exit 0
fi
echo KAFKA_AVAILABLE
"""
        output = self.kubectl.exec_command_checked(
            shlex.join(["kubectl", "exec", "-n", self.namespace, self.name, "-c", "client", "--", "sh", "-c", script]),
            timeout=45,
        )
        lines = output.splitlines()
        return "KAFKA_UNAVAILABLE" not in lines and "KAFKA_AVAILABLE" in lines and token in lines

    def wait_until_available(self, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            if self.check():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(2)
