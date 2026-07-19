from types import SimpleNamespace

from sregym.generators.fault.inject_kafka import (
    CONSUMER_SCRIPT,
    PRODUCER_SCRIPT,
    KafkaBrokerClient,
    KafkaFaultInjector,
)


class _RecordingKubeCtl:
    def __init__(self):
        self.commands = []

    def list_pods(self, namespace):
        pods = [
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="kafka-abc123",
                    labels={"app.kubernetes.io/name": "kafka"},
                    deletion_timestamp=None,
                ),
                status=SimpleNamespace(phase="Running"),
            ),
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="order-stream-abc123",
                    labels={"app": "order-stream"},
                    deletion_timestamp=None,
                ),
                status=SimpleNamespace(phase="Running"),
            ),
        ]
        return SimpleNamespace(items=pods)

    def exec_command(self, command, input_data=None):
        self.commands.append((command, input_data))
        return "\n__STREAM_ADMIN_OK__\n"


def test_pipeline_scripts_do_not_publish_injection_or_hidden_repair_controls():
    scripts = CONSUMER_SCRIPT + PRODUCER_SCRIPT

    assert "LENIENT" not in scripts
    assert "POISON" not in scripts.upper()
    assert "SEED_RECORD_COUNT" not in scripts
    assert "delete_topics" not in scripts
    assert "reset_consumer_group" not in scripts


def test_broker_commands_use_bounded_heap_and_stream_records_over_stdin():
    kubectl = _RecordingKubeCtl()
    broker = KafkaBrokerClient(kubectl, "astronomy-shop")

    broker.publish_lines("orders-fulfillment", ['{"order_id":"ORD-1"}'])

    command, input_data = kubectl.commands[0]
    assert "kubectl exec -i" in command
    assert "KAFKA_HEAP_OPTS=-Xms32m -Xmx64m" in command
    assert "KAFKA_OPTS=" in command
    assert "kafka-console-producer.sh" in command
    assert input_data == '{"order_id":"ORD-1"}\n'
    assert "ORD-1" not in command


def test_recovery_resets_inactive_group_without_patching_consumer_template():
    events = []
    injector = object.__new__(KafkaFaultInjector)
    injector.blocked_offset = 20
    injector.broker = SimpleNamespace(
        reset_group_offset=lambda group, topic, offset: events.append(("reset", group, topic, offset))
    )
    injector._scale_consumer = lambda replicas: events.append(("scale", replicas))
    injector._wait_consumer_pods_gone = lambda: events.append(("gone",))
    injector._wait_deployment_ready = lambda name: events.append(("ready", name))

    injector.recover()

    assert events == [
        ("scale", 0),
        ("gone",),
        ("reset", "orders-validator", "orders-fulfillment", 21),
        ("scale", 1),
        ("ready", "orders-validator"),
    ]
