"""Build and operate a small Kafka-backed order validation pipeline."""

import base64
import json
import logging
import shlex
import time

from kubernetes import client

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.sregym.inject_kafka")
logger.propagate = True
logger.setLevel(logging.DEBUG)


CONSUMER_SCRIPT = r"""
import json
import logging
import os
import signal
import time

from confluent_kafka import Consumer, Producer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orders-validator")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC = os.environ.get("ORDERS_TOPIC", "orders-fulfillment")
OUTPUT_TOPIC = os.environ.get("OUTPUT_TOPIC", "orders-processed")
GROUP = os.environ.get("CONSUMER_GROUP", "orders-validator")

_shutdown = False


def _request_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def process(value):
    obj = json.loads(value.decode("utf-8"))
    if "order_id" not in obj:
        raise ValueError("record has no 'order_id' field")
    return obj


def main():
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": GROUP,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": 86400000,
        }
    )
    out = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
    consumer.subscribe([TOPIC])
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    log.info("orders-validator started bootstrap=%s topic=%s group=%s", BOOTSTRAP, TOPIC, GROUP)

    first = True
    while not _shutdown:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            log.warning("consumer error: %s", msg.error())
            continue

        if first:
            log.info("resuming partition at offset=%d", msg.offset())
            first = False

        try:
            order = process(msg.value())
        except Exception as exc:
            log.error("record validation failed at offset=%d: %s", msg.offset(), exc)
            log.error("partition processing paused at offset=%d", msg.offset())
            blocked = 0
            while not _shutdown:
                time.sleep(1)
                blocked += 1
                if blocked % 15 == 0:
                    log.error("partition remains paused at offset=%d", msg.offset())
            break

        result = json.dumps(
            {"source_offset": msg.offset(), "order_id": order["order_id"]},
            separators=(",", ":"),
        )
        out.produce(OUTPUT_TOPIC, value=result.encode("utf-8"))
        out.flush(10)
        consumer.commit(message=msg, asynchronous=False)
        log.info("processed order_id=%s committed_offset=%d", order["order_id"], msg.offset() + 1)

    out.flush(10)
    consumer.close()
    log.info("orders-validator stopped")


if __name__ == "__main__":
    main()
"""


PRODUCER_SCRIPT = r"""
import json
import logging
import os
import time
import uuid

from confluent_kafka import Producer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("order-stream")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC = os.environ.get("ORDERS_TOPIC", "orders-fulfillment")
INTERVAL = float(os.environ.get("PRODUCE_INTERVAL_SEC", "2.0"))


def main():
    producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
    while True:
        order_id = "ORD-" + uuid.uuid4().hex[:12].upper()
        record = json.dumps({"order_id": order_id, "amount": 1 + int(uuid.uuid4().hex[:2], 16) % 7})
        producer.produce(TOPIC, value=record.encode("utf-8"))
        producer.poll(0)
        producer.flush(10)
        log.info("published order_id=%s", order_id)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
"""


class KafkaBrokerClient:
    """Run bounded Kafka administration and data-plane inspection commands."""

    _BIN_DIR = "/opt/kafka/bin"
    _SUCCESS_MARKER = "__STREAM_ADMIN_OK__"
    _BROKER_LABEL = "app.kubernetes.io/name"
    _BROKER_LABEL_VALUE = "kafka"

    def __init__(self, kubectl: KubeCtl, namespace: str):
        self.kubectl = kubectl
        self.namespace = namespace

    def _broker_pod_name(self) -> str:
        pods = self.kubectl.list_pods(self.namespace).items
        candidates = [
            pod
            for pod in pods
            if (pod.metadata.labels or {}).get(self._BROKER_LABEL) == self._BROKER_LABEL_VALUE
            and pod.status.phase == "Running"
            and pod.metadata.deletion_timestamp is None
        ]
        if not candidates:
            raise RuntimeError("no running Kafka broker pod found")
        return candidates[0].metadata.name

    def _pipeline_pod_name(self) -> str:
        pods = self.kubectl.list_pods(self.namespace).items
        candidates = [
            pod
            for pod in pods
            if (pod.metadata.labels or {}).get("app") == "order-stream"
            and pod.status.phase == "Running"
            and pod.metadata.deletion_timestamp is None
        ]
        if not candidates:
            raise RuntimeError("no running order-stream pod found")
        return candidates[0].metadata.name

    def _run(self, script: str, args: list[str], input_data: str | None = None) -> str:
        if not script.startswith("kafka-") or not script.endswith(".sh"):
            raise ValueError(f"unsupported Kafka command: {script}")

        command = ["kubectl", "exec"]
        if input_data is not None:
            command.append("-i")
        command.extend(
            [
                "-n",
                self.namespace,
                self._broker_pod_name(),
                "--",
                "env",
                "KAFKA_HEAP_OPTS=-Xms32m -Xmx64m",
                "KAFKA_OPTS=",
                f"{self._BIN_DIR}/{script}",
                *args,
            ]
        )
        shell_command = " ".join(shlex.quote(part) for part in command)
        shell_command += f" && printf '\\n{self._SUCCESS_MARKER}\\n'"
        output = self.kubectl.exec_command(shell_command, input_data=input_data)
        if self._SUCCESS_MARKER not in output:
            raise RuntimeError(f"Kafka command {script} failed: {output.strip()[-1000:]}")
        return output.replace(self._SUCCESS_MARKER, "").strip()

    def delete_group(self, group: str) -> None:
        try:
            self._run(
                "kafka-consumer-groups.sh",
                ["--bootstrap-server", "kafka:9092", "--delete", "--group", group],
            )
        except RuntimeError as exc:
            if "does not exist" not in str(exc) and "not found" not in str(exc):
                raise

    def recreate_topic(self, topic: str) -> None:
        try:
            self._run(
                "kafka-topics.sh",
                ["--bootstrap-server", "kafka:9092", "--delete", "--topic", topic],
            )
            time.sleep(2)
        except RuntimeError as exc:
            if "does not exist" not in str(exc) and "UnknownTopicOrPartition" not in str(exc):
                raise

        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                self._run(
                    "kafka-topics.sh",
                    [
                        "--bootstrap-server",
                        "kafka:9092",
                        "--create",
                        "--topic",
                        topic,
                        "--partitions",
                        "1",
                        "--replication-factor",
                        "1",
                    ],
                )
                return
            except RuntimeError as exc:
                if "already exists" not in str(exc) and "marked for deletion" not in str(exc):
                    raise
                time.sleep(2)
        raise TimeoutError(f"Kafka topic {topic!r} was not recreated within 60 seconds")

    def publish_lines(self, topic: str, records: list[str]) -> None:
        self._run(
            "kafka-console-producer.sh",
            ["--bootstrap-server", "kafka:9092", "--topic", topic],
            input_data="\n".join(records) + "\n",
        )

    def reset_group_offset(self, group: str, topic: str, offset: int) -> None:
        output = self._run(
            "kafka-consumer-groups.sh",
            [
                "--bootstrap-server",
                "kafka:9092",
                "--group",
                group,
                "--topic",
                f"{topic}:0",
                "--reset-offsets",
                "--to-offset",
                str(offset),
                "--execute",
            ],
        )
        if topic not in output or str(offset) not in output:
            raise RuntimeError(f"Kafka did not confirm the requested group offset: {output}")

    def pipeline_snapshot(self, source_topic: str, output_topic: str, group: str) -> dict:
        """Read both topics and the committed offset from an application pod.

        The broker has a tight 600 MiB limit and already reserves a 400 MiB JVM
        heap. Repeatedly launching Kafka's Java CLI inside that container can
        OOM the broker even when the CLI heap is constrained. The ordinary
        producer already has the native Python Kafka client installed, so
        read-only evaluation runs there in one bounded process instead.
        """
        probe = r"""
import base64
import json
import sys
import uuid

from confluent_kafka import Consumer, TopicPartition

source_topic, output_topic, target_group = sys.argv[1:4]


def read_partition(topic):
    consumer = Consumer(
        {
            "bootstrap.servers": "kafka:9092",
            "group.id": "orders-read-" + uuid.uuid4().hex,
            "enable.auto.commit": False,
        }
    )
    partition = TopicPartition(topic, 0, 0)
    consumer.assign([partition])
    _, high = consumer.get_watermark_offsets(partition, timeout=10, cached=False)
    records = []
    while len(records) < high:
        message = consumer.poll(2.0)
        if message is None:
            raise RuntimeError("timed out reading " + topic)
        if message.error():
            raise RuntimeError(str(message.error()))
        if message.offset() >= high:
            break
        records.append(
            {
                "offset": message.offset(),
                "value": base64.b64encode(message.value() or b"").decode("ascii"),
            }
        )
    consumer.close()
    return records


position_reader = Consumer(
    {
        "bootstrap.servers": "kafka:9092",
        "group.id": target_group,
        "enable.auto.commit": False,
    }
)
position = position_reader.committed([TopicPartition(source_topic, 0)], timeout=10)[0].offset
position_reader.close()

# Capture the output high-watermark first. Any result present there must have
# a corresponding source record, and the subsequent source read will include
# it even while the live pipeline continues advancing.
output_records = read_partition(output_topic)
source_records = read_partition(source_topic)
state = {
    "source": source_records,
    "output": output_records,
    "group_offset": None if position < 0 else position,
}
print("STREAM_STATE=" + json.dumps(state, separators=(",", ":")))
"""
        encoded = base64.b64encode(probe.encode("utf-8")).decode("ascii")
        python = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = [
            "kubectl",
            "exec",
            "-n",
            self.namespace,
            self._pipeline_pod_name(),
            "--",
            "python",
            "-c",
            python,
            source_topic,
            output_topic,
            group,
        ]
        shell_command = " ".join(shlex.quote(part) for part in command)
        shell_command += f" && printf '\\n{self._SUCCESS_MARKER}\\n'"
        output = self.kubectl.exec_command(shell_command)
        if self._SUCCESS_MARKER not in output:
            raise RuntimeError(f"Kafka pipeline inspection failed: {output.strip()[-1000:]}")
        for line in output.splitlines():
            if line.startswith("STREAM_STATE="):
                state = json.loads(line.removeprefix("STREAM_STATE="))
                for key in ("source", "output"):
                    for record in state[key]:
                        record["value"] = base64.b64decode(record["value"]).decode("utf-8")
                return state
        raise RuntimeError("Kafka pipeline inspection returned no state")


class KafkaFaultInjector(FaultInjector):
    """Create a Kafka order stream whose consumer is blocked by one invalid record."""

    TOPIC = "orders-fulfillment"
    OUTPUT_TOPIC = "orders-processed"
    CONSUMER_GROUP = "orders-validator"
    CONSUMER_DEPLOYMENT = "orders-validator"
    PRODUCER_DEPLOYMENT = "order-stream"
    LEGACY_ARCHIVER_DEPLOYMENT = "orders-archiver"
    SCRIPTS_CONFIGMAP = "orders-pipeline-scripts"

    INITIAL_RECORD_COUNT = 20
    INVALID_RECORD = '{"order_id":"ORD-100020","amount":'

    PIPELINE_IMAGE = "python:3.12-slim"
    CONFLUENT_KAFKA_VERSION = "2.5.3"

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.broker = KafkaBrokerClient(self.kubectl, namespace)
        self.blocked_offset = self.INITIAL_RECORD_COUNT

    @classmethod
    def initial_records(cls) -> list[str]:
        return [
            json.dumps({"order_id": f"ORD-{100000 + index}", "amount": (index % 7) + 1})
            for index in range(cls.INITIAL_RECORD_COUNT)
        ]

    def inject(self) -> int:
        logger.info("[Kafka pipeline] Removing any previous order-stream workloads")
        for name in (
            self.PRODUCER_DEPLOYMENT,
            self.CONSUMER_DEPLOYMENT,
            self.LEGACY_ARCHIVER_DEPLOYMENT,
        ):
            self._delete_deployment(name)
        self._wait_pipeline_pods_gone()

        logger.info("[Kafka pipeline] Creating fresh topics and consumer position")
        self.broker.delete_group(self.CONSUMER_GROUP)
        self.broker.recreate_topic(self.TOPIC)
        self.broker.recreate_topic(self.OUTPUT_TOPIC)

        records = [*self.initial_records(), self.INVALID_RECORD]
        self.broker.publish_lines(self.TOPIC, records)

        logger.info("[Kafka pipeline] Applying ordinary producer and validator workloads")
        self._apply_configmap()
        self._apply_pipeline_deployment(name=self.PRODUCER_DEPLOYMENT, script="producer.py")
        self._wait_deployment_ready(self.PRODUCER_DEPLOYMENT)
        self._apply_pipeline_deployment(name=self.CONSUMER_DEPLOYMENT, script="consumer.py")
        self._wait_deployment_ready(self.CONSUMER_DEPLOYMENT)
        self._wait_for_log(self.CONSUMER_DEPLOYMENT, "partition processing paused", timeout=420)
        logger.info("[Kafka pipeline] Validator is paused at source offset %d", self.blocked_offset)
        return self.blocked_offset

    def recover(self) -> None:
        """Move the inactive consumer group past the invalid record, then restart it."""
        logger.info("[Kafka pipeline] Recovery: stopping the validator")
        self._scale_consumer(0)
        self._wait_consumer_pods_gone()
        next_offset = self.blocked_offset + 1
        self.broker.reset_group_offset(self.CONSUMER_GROUP, self.TOPIC, next_offset)
        logger.info("[Kafka pipeline] Consumer group advanced to offset %d", next_offset)
        self._scale_consumer(1)
        self._wait_deployment_ready(self.CONSUMER_DEPLOYMENT)

    def _scale_consumer(self, replicas: int) -> None:
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            self.CONSUMER_DEPLOYMENT, self.namespace, {"spec": {"replicas": replicas}}
        )

    def _pipeline_pods(self, names: set[str]):
        return [
            pod
            for pod in self.kubectl.list_pods(self.namespace).items
            if (pod.metadata.labels or {}).get("app") in names
        ]

    def _wait_pipeline_pods_gone(self, timeout: int = 120) -> None:
        names = {
            self.PRODUCER_DEPLOYMENT,
            self.CONSUMER_DEPLOYMENT,
            self.LEGACY_ARCHIVER_DEPLOYMENT,
        }
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pipeline_pods(names):
                return
            time.sleep(3)
        raise TimeoutError("previous Kafka pipeline pods did not terminate")

    def _wait_consumer_pods_gone(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pipeline_pods({self.CONSUMER_DEPLOYMENT}):
                return
            time.sleep(3)
        raise TimeoutError("orders-validator pods did not terminate")

    def _apply_configmap(self) -> None:
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=self.SCRIPTS_CONFIGMAP, namespace=self.namespace),
            data={"consumer.py": CONSUMER_SCRIPT, "producer.py": PRODUCER_SCRIPT},
        )
        api = self.kubectl.core_v1_api
        try:
            api.create_namespaced_config_map(self.namespace, body)
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise
            api.replace_namespaced_config_map(self.SCRIPTS_CONFIGMAP, self.namespace, body)

    def _apply_pipeline_deployment(self, name: str, script: str) -> None:
        install_cmd = (
            f"pip install --no-cache-dir --quiet --retries 5 "
            f"confluent-kafka=={self.CONFLUENT_KAFKA_VERSION} && exec python /scripts/{script}"
        )
        env = [
            {"name": "KAFKA_BOOTSTRAP", "value": "kafka:9092"},
            {"name": "ORDERS_TOPIC", "value": self.TOPIC},
        ]
        if name == self.CONSUMER_DEPLOYMENT:
            env.extend(
                [
                    {"name": "OUTPUT_TOPIC", "value": self.OUTPUT_TOPIC},
                    {"name": "CONSUMER_GROUP", "value": self.CONSUMER_GROUP},
                ]
            )

        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": self.namespace, "labels": {"app": name}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "containers": [
                            {
                                "name": name,
                                "image": self.PIPELINE_IMAGE,
                                "command": ["sh", "-lc", install_cmd],
                                "env": env,
                                "volumeMounts": [{"name": "scripts", "mountPath": "/scripts"}],
                            }
                        ],
                        "volumes": [{"name": "scripts", "configMap": {"name": self.SCRIPTS_CONFIGMAP}}],
                    },
                },
            },
        }

        api = self.kubectl.apps_v1_api
        try:
            api.create_namespaced_deployment(self.namespace, manifest)
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise
            self._delete_deployment(name)
            for _ in range(30):
                try:
                    api.read_namespaced_deployment(name, self.namespace)
                    time.sleep(2)
                except client.exceptions.ApiException as read_exc:
                    if read_exc.status == 404:
                        break
                    raise
            api.create_namespaced_deployment(self.namespace, manifest)

    def _delete_deployment(self, name: str) -> None:
        try:
            self.kubectl.apps_v1_api.delete_namespaced_deployment(name, self.namespace)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                logger.warning("[Kafka pipeline] delete deployment %s: %r", name, exc)

    def _wait_deployment_ready(self, name: str, timeout: int = 420) -> None:
        api = self.kubectl.apps_v1_api
        deadline = time.time() + timeout
        while time.time() < deadline:
            dep = api.read_namespaced_deployment(name, self.namespace)
            desired = dep.spec.replicas or 1
            if (dep.status.ready_replicas or 0) >= desired:
                logger.info("[Kafka pipeline] Deployment '%s' is ready", name)
                return
            time.sleep(5)
        raise TimeoutError(
            f"Deployment '{name}' not ready within {timeout}s "
            f"(check cluster egress to PyPI for the confluent-kafka install)"
        )

    def _wait_for_log(self, deployment: str, substring: str, timeout: int) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.kubectl.exec_command(f"kubectl logs deployment/{deployment} -n {self.namespace} --tail=400")
            if substring in out:
                return
            time.sleep(5)
        raise TimeoutError(f"'{substring}' not seen in '{deployment}' logs within {timeout}s")
