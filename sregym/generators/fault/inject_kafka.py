"""Inject a data-plane (Kafka) poison-pill / head-of-line-block fault."""

import logging
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
LENIENT = os.environ.get("LENIENT", "").lower() in ("1", "true", "yes")

_shutdown = False


def _request_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def process(value):
    obj = json.loads(value.decode("utf-8"))
    if "order_id" not in obj:
        raise ValueError("record has no 'order_id' field")
    return obj["order_id"]


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
            log.info("RESUMING FROM offset=%d", msg.offset())
            first = False

        try:
            order_id = process(msg.value())
        except Exception as exc:
            log.error("unprocessable record at offset=%d: %s", msg.offset(), exc)
            if not LENIENT:
                log.error("cannot advance past offset=%d -- head-of-line block", msg.offset())
                blocked = 0
                while not _shutdown:
                    time.sleep(1)
                    blocked += 1
                    if blocked % 15 == 0:
                        log.error("still blocked on unprocessable record at offset=%d", msg.offset())
                break
            consumer.commit(message=msg, asynchronous=False)
            log.info("skipped unprocessable record COMMITTED offset=%d", msg.offset() + 1)
            continue

        out.produce(OUTPUT_TOPIC, value=str(msg.offset()).encode("utf-8"))
        out.flush(10)
        consumer.commit(message=msg, asynchronous=False)
        log.info("processed order_id=%s COMMITTED offset=%d", order_id, msg.offset() + 1)

    out.flush(10)
    consumer.close()
    log.info("orders-validator stopped; left consumer group")


if __name__ == "__main__":
    main()
"""

PRODUCER_SCRIPT = r"""
import json
import logging
import os
import time

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("order-stream")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC = os.environ.get("ORDERS_TOPIC", "orders-fulfillment")
OUTPUT_TOPIC = os.environ.get("OUTPUT_TOPIC", "orders-processed")
GROUP = os.environ.get("CONSUMER_GROUP", "orders-validator")
SEED_COUNT = int(os.environ.get("SEED_RECORD_COUNT", "20"))
POISON_RECORD = os.environ.get("POISON_RECORD", "CORRUPTED-RECORD-NOT-VALID-JSON-POISON-PILL")
INTERVAL = float(os.environ.get("PRODUCE_INTERVAL_SEC", "2.0"))


def wait_for_broker(admin):
    for _ in range(60):
        try:
            if admin.list_topics(timeout=10).brokers:
                return
        except Exception as exc:
            log.info("waiting for kafka broker: %s", exc)
        time.sleep(5)
    raise RuntimeError("kafka broker not reachable at " + BOOTSTRAP)


def reset_consumer_group(admin):
    try:
        for _, fut in admin.delete_consumer_groups([GROUP]).items():
            try:
                fut.result()
                log.info("deleted stale consumer group %s", GROUP)
            except Exception as exc:
                log.info("consumer group %s not deleted (ok): %s", GROUP, exc)
    except Exception as exc:
        log.info("delete_consumer_groups skipped (ok): %s", exc)


def ensure_fresh_topic(admin, topic):
    if topic in admin.list_topics(timeout=20).topics:
        log.info("deleting existing topic %s", topic)
        for _, fut in admin.delete_topics([topic], operation_timeout=30).items():
            try:
                fut.result()
            except Exception as exc:
                log.warning("delete_topics: %s", exc)
        for _ in range(30):
            if topic not in admin.list_topics(timeout=20).topics:
                break
            time.sleep(2)

    for attempt in range(10):
        for _, fut in admin.create_topics(
            [NewTopic(topic, num_partitions=1, replication_factor=1)]
        ).items():
            try:
                fut.result()
            except Exception as exc:
                log.warning("create_topics %s attempt %d: %s", topic, attempt + 1, exc)
        if topic in admin.list_topics(timeout=20).topics:
            log.info("topic %s is ready (1 partition)", topic)
            return
        time.sleep(3)
    raise RuntimeError("could not create fresh topic " + topic)


def main():
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    wait_for_broker(admin)
    reset_consumer_group(admin)
    ensure_fresh_topic(admin, TOPIC)
    ensure_fresh_topic(admin, OUTPUT_TOPIC)

    producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})

    for i in range(SEED_COUNT):
        record = json.dumps({"order_id": "seed-%d" % i, "amount": (i % 7) + 1})
        producer.produce(TOPIC, value=record.encode("utf-8"))
    producer.produce(TOPIC, value=POISON_RECORD.encode("utf-8"))
    producer.flush(30)
    log.info("SEED COMPLETE seeded=%d poison_offset=%d", SEED_COUNT, SEED_COUNT)

    seq = SEED_COUNT + 1
    while True:
        record = json.dumps({"order_id": "ord-%d" % seq, "amount": (seq % 7) + 1})
        producer.produce(TOPIC, value=record.encode("utf-8"))
        producer.poll(0)
        producer.flush(5)
        log.info("produced order_id=ord-%d", seq)
        seq += 1
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
"""

ARCHIVER_SCRIPT = r"""
import logging
import os
import uuid

from confluent_kafka import Consumer, TopicPartition

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orders-archiver")

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
OUTPUT_TOPIC = os.environ.get("OUTPUT_TOPIC", "orders-processed")


def main():
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": "orders-archiver-" + uuid.uuid4().hex,
            "enable.auto.commit": False,
        }
    )
    consumer.assign([TopicPartition(OUTPUT_TOPIC, 0, 0)])
    log.info("orders-archiver started bootstrap=%s topic=%s", BOOTSTRAP, OUTPUT_TOPIC)
    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            log.warning("archiver error: %s", msg.error())
            continue
        log.info("AUDIT src_offset=%s", msg.value().decode("utf-8"))


if __name__ == "__main__":
    main()
"""


class KafkaFaultInjector(FaultInjector):
    """Injects a poison-pill / head-of-line-block fault into a Kafka stream."""

    TOPIC = "orders-fulfillment"
    OUTPUT_TOPIC = "orders-processed"
    CONSUMER_GROUP = "orders-validator"
    CONSUMER_DEPLOYMENT = "orders-validator"
    PRODUCER_DEPLOYMENT = "order-stream"
    ARCHIVER_DEPLOYMENT = "orders-archiver"
    SCRIPTS_CONFIGMAP = "orders-pipeline-scripts"

    SEED_RECORD_COUNT = 20
    POISON_RECORD = "CORRUPTED-RECORD-NOT-VALID-JSON-POISON-PILL"

    PIPELINE_IMAGE = "python:3.12-slim"
    CONFLUENT_KAFKA_VERSION = "2.5.3"

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.poison_offset = self.SEED_RECORD_COUNT

    def inject(self) -> int:
        logger.info("[Kafka FI] Applying pipeline scripts ConfigMap")
        self._apply_configmap()

        logger.info("[Kafka FI] Deploying producer (creates topics + seeds poison record)")
        self._apply_pipeline_deployment(
            name=self.PRODUCER_DEPLOYMENT,
            script="producer.py",
            extra_env=[
                {"name": "SEED_RECORD_COUNT", "value": str(self.SEED_RECORD_COUNT)},
                {"name": "POISON_RECORD", "value": self.POISON_RECORD},
            ],
        )
        self._wait_deployment_ready(self.PRODUCER_DEPLOYMENT)
        logger.info("[Kafka FI] Waiting for the producer to finish seeding the topics")
        self._wait_for_log(self.PRODUCER_DEPLOYMENT, "SEED COMPLETE", timeout=420)

        logger.info("[Kafka FI] Deploying archiver (records processed offsets)")
        self._apply_pipeline_deployment(name=self.ARCHIVER_DEPLOYMENT, script="archiver.py", extra_env=[])
        self._wait_deployment_ready(self.ARCHIVER_DEPLOYMENT)

        logger.info("[Kafka FI] Deploying consumer (will stall on the poison record)")
        self._apply_pipeline_deployment(name=self.CONSUMER_DEPLOYMENT, script="consumer.py", extra_env=[])
        self._wait_deployment_ready(self.CONSUMER_DEPLOYMENT)
        logger.info("[Kafka FI] Waiting for the consumer to stall on the poison record")
        self._wait_for_log(self.CONSUMER_DEPLOYMENT, "head-of-line block", timeout=420)
        logger.info("[Kafka FI] Fault is live; consumer is halted at offset %d", self.poison_offset)
        return self.poison_offset

    def recover(self) -> None:
        """Recover by switching the consumer to lenient (skip-poison) mode.

        Scaling the consumer to zero before patching avoids a rolling update,
        which would otherwise surge a temporary pod that advances the offset.
        With replicas at zero the patch only updates the template; scaling back
        to one then starts a single lenient pod that resumes at the poison
        offset, skips it, and drains the backlog. The producer and archiver are
        left running; all injected objects are removed at namespace teardown.
        """
        logger.info("[Kafka FI] Recovery: switching consumer to lenient skip-poison mode")
        self._scale_consumer(0)
        self._wait_consumer_pods_gone()
        self._patch_consumer_lenient()
        self._scale_consumer(1)
        self._wait_deployment_ready(self.CONSUMER_DEPLOYMENT)
        logger.info("[Kafka FI] Recovery complete: consumer skips the poison record and the pipeline drains")

    def _scale_consumer(self, replicas: int) -> None:
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            self.CONSUMER_DEPLOYMENT, self.namespace, {"spec": {"replicas": replicas}}
        )

    def _wait_consumer_pods_gone(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pods = [
                pod
                for pod in self.kubectl.list_pods(self.namespace).items
                if (pod.metadata.labels or {}).get("app") == self.CONSUMER_DEPLOYMENT
            ]
            if not pods:
                return
            time.sleep(3)

    def _patch_consumer_lenient(self) -> None:
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": self.CONSUMER_DEPLOYMENT,
                                "env": [{"name": "LENIENT", "value": "true"}],
                            }
                        ]
                    }
                }
            }
        }
        self.kubectl.apps_v1_api.patch_namespaced_deployment(self.CONSUMER_DEPLOYMENT, self.namespace, patch)

    def _apply_configmap(self) -> None:
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=self.SCRIPTS_CONFIGMAP, namespace=self.namespace),
            data={
                "consumer.py": CONSUMER_SCRIPT,
                "producer.py": PRODUCER_SCRIPT,
                "archiver.py": ARCHIVER_SCRIPT,
            },
        )
        api = self.kubectl.core_v1_api
        try:
            api.create_namespaced_config_map(self.namespace, body)
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise
            api.replace_namespaced_config_map(self.SCRIPTS_CONFIGMAP, self.namespace, body)

    def _apply_pipeline_deployment(self, name: str, script: str, extra_env: list[dict]) -> None:
        install_cmd = (
            f"pip install --no-cache-dir --quiet --retries 5 "
            f"confluent-kafka=={self.CONFLUENT_KAFKA_VERSION} && exec python /scripts/{script}"
        )
        env = [
            {"name": "KAFKA_BOOTSTRAP", "value": "kafka:9092"},
            {"name": "ORDERS_TOPIC", "value": self.TOPIC},
            {"name": "OUTPUT_TOPIC", "value": self.OUTPUT_TOPIC},
            {"name": "CONSUMER_GROUP", "value": self.CONSUMER_GROUP},
        ] + extra_env

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
                logger.warning("[Kafka FI] delete deployment %s: %r", name, exc)

    def _wait_deployment_ready(self, name: str, timeout: int = 420) -> None:
        api = self.kubectl.apps_v1_api
        deadline = time.time() + timeout
        while time.time() < deadline:
            dep = api.read_namespaced_deployment(name, self.namespace)
            desired = dep.spec.replicas or 1
            if (dep.status.ready_replicas or 0) >= desired:
                logger.info("[Kafka FI] Deployment '%s' is ready", name)
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
