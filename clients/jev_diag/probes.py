"""Read-only commands run inside pods (enabled with --exec-probes).

Both uses are keyed by backend roles (derive.ROLE_SYNONYMS), not by any particular application:

* Consumer progress on a message broker, sampled twice. A partition whose committed offset does not move while
  its log end grows, and that has an active consumer, is stalled at that offset. The group, topic, and offset
  come from the broker; the consumer is mapped to a component by its pod IP.
* A protocol health command inside a backend that looks healthy while a client's errors name it (PING,
  pg_isready, a mongo ping). The output is mapped to a meaning in code, and the client's credential settings are
  read from its env var names (values are never read).

A command runs only when the target container has room for it: its memory limit minus its current usage must
cover the tool's footprint. A second process in a container near its memory limit gets the container OOM-killed,
which would break the system being diagnosed (a JVM command-line tool inside a memory-capped Kafka broker did
exactly that), so heavy tools are skipped in capped containers.
"""

from __future__ import annotations

import logging
import re
import time

from clients.jev_diag.checks import parse_memory_bytes
from clients.jev_diag.collector import ClusterSnapshot, run_kubectl_capture, trim
from clients.jev_diag.derive import _component_tokens, backend_role
from clients.jev_diag.timeutil import now, parse_time, seconds_between

logger = logging.getLogger("all.jev_diag.probes")

EXEC_TIMEOUT = 20
DEFAULT_BROKER_INTERVAL = 15
# Memory a command needs in the target container, beyond the container's current usage.
NATIVE_TOOL_BYTES = 8 * 1024**2  # valkey-cli, redis-cli, pg_isready, mysqladmin: a few MB of RSS
SHELL_TOOL_BYTES = 256 * 1024**2  # mongosh (Node.js)
JVM_TOOL_BYTES = 768 * 1024**2  # Kafka command-line tools start a JVM

_DEFAULT_PORTS = {"redis": 6379, "mongodb": 27017, "postgresql": 5432, "mysql": 3306, "kafka": 9092}


def _pick_pod(comp: dict) -> tuple[str | None, str | None]:
    pods = [
        p for p in comp.get("pods") or [] if p.get("phase") == "Running" and p.get("ready", "0/0").split("/")[0] != "0"
    ]
    containers = [c for c in comp.get("containers") or [] if not c.get("init")]
    if not pods or not containers:
        return None, None
    tokens = _component_tokens(comp)
    container = next(
        (c for c in containers if any(t in (c.get("image") or "").lower() for t in tokens if len(t) >= 4)),
        containers[0],
    )
    return pods[0]["name"], container.get("name")


def _port(comp: dict, container: str | None, role: str) -> int:
    for c in comp.get("containers") or []:
        if c.get("name") == container and c.get("ports"):
            default = _DEFAULT_PORTS.get(role)
            return default if default in c["ports"] else c["ports"][0]
    return _DEFAULT_PORTS.get(role, 0)


def headroom_ok(comp: dict, pod: str, container: str | None, need: int) -> tuple[bool, str]:
    """Whether the container can host a command needing `need` bytes, and why not when it cannot.

    Without a memory limit the container's cgroup is not the constraint. With a limit and unknown usage, only a
    small native tool may run.
    """
    limits = next((c.get("limits") or {} for c in comp.get("containers") or [] if c.get("name") == container), {})
    limit = parse_memory_bytes(limits.get("memory"))
    if limit is None:
        return True, ""
    usage = next(
        (
            parse_memory_bytes(u.get("memory"))
            for u in comp.get("resource_usage") or []
            if u.get("pod") == pod and u.get("container") == container
        ),
        None,
    )
    if usage is None:
        ok = need <= NATIVE_TOOL_BYTES
        return ok, "" if ok else f"memory usage unknown under a {limits.get('memory')} limit"
    ok = limit - usage >= need
    return ok, "" if ok else f"only {int((limit - usage) / 1024**2)}Mi free under a {limits.get('memory')} limit"


def run_in_pod(namespace: str, pod: str, container: str | None, command: list[str]) -> tuple[int | None, str]:
    args = ["exec", "-n", namespace, pod] + (["-c", container] if container else []) + ["--", *command]
    rc, out, err = run_kubectl_capture(args, timeout=EXEC_TIMEOUT)
    return rc, (out + ("\n" + err if err else "")).strip()


def _missing_binary(rc: int | None, text: str) -> bool:
    return rc is None or rc in (126, 127) or "executable file not found" in text or "not found" in text.lower()[:200]


# --------------------------------------------------------------------------- broker consumer progress

_KAFKA_GROUP_COMMANDS = (
    "kafka-consumer-groups.sh",
    "/opt/kafka/bin/kafka-consumer-groups.sh",
    "/opt/bitnami/kafka/bin/kafka-consumer-groups.sh",
    "kafka-consumer-groups",
)


def parse_consumer_groups(text: str) -> list[dict]:
    """Rows of `kafka-consumer-groups --describe --all-groups` output."""
    rows, header = [], None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "GROUP":
            header = parts
            continue
        if header and len(parts) >= min(len(header), 6) and not line.startswith(("Consumer group", "Warning")):
            rows.append(dict(zip(header, parts, strict=False)))
    return rows


def _kafka_sample(namespace: str, pod: str, container: str | None, port: int) -> tuple[str | None, list[dict]]:
    for binary in _KAFKA_GROUP_COMMANDS:
        cmd = [binary, "--bootstrap-server", f"localhost:{port}", "--describe", "--all-groups"]
        rc, text = run_in_pod(namespace, pod, container, cmd)
        if rc == 0 and "GROUP" in text:
            return binary, parse_consumer_groups(text)
        if not _missing_binary(rc, text):
            logger.info("consumer-group command failed in %s/%s: %s", namespace, pod, trim(text, 160))
            return binary, []
    return None, []


def broker_samples(snapshot: ClusterSnapshot, skipped: dict | None = None) -> dict:
    """One sample of consumer-group positions for every Kafka-role component (empty when none answer).

    The consumer-group tool starts a JVM, so it runs only in a broker container with JVM_TOOL_BYTES free.
    """
    samples: dict[str, dict] = {}
    for cid, comp in snapshot.components.items():
        if backend_role(comp) != "kafka":
            continue
        pod, container = _pick_pod(comp)
        if not pod:
            continue
        ok, why = headroom_ok(comp, pod, container, JVM_TOOL_BYTES)
        if not ok:
            if skipped is not None:
                skipped[cid] = f"consumer-group tool skipped: {why}"
            continue
        binary, rows = _kafka_sample(comp["namespace"], pod, container, _port(comp, container, "kafka"))
        if binary:
            samples[cid] = {"at": now().isoformat(), "pod": pod, "container": container, "binary": binary, "rows": rows}
    return samples


def apply_broker_progress(snapshot: ClusterSnapshot, before: dict, after: dict) -> list[dict]:
    """Compare two samples; attach a signal to the consumer of every stalled partition."""
    by_ip = {
        p.get("ip"): cid for cid, comp in snapshot.components.items() for p in comp.get("pods") or [] if p.get("ip")
    }
    findings = []
    for broker, sample in after.items():
        prior = {
            (r.get("GROUP"), r.get("TOPIC"), r.get("PARTITION")): r for r in (before.get(broker) or {}).get("rows", [])
        }
        interval = seconds_between(parse_time(sample.get("at")), parse_time((before.get(broker) or {}).get("at")))
        for row in sample.get("rows", []):
            key = (row.get("GROUP"), row.get("TOPIC"), row.get("PARTITION"))
            old = prior.get(key)
            cur_off, end, consumer = row.get("CURRENT-OFFSET"), row.get("LOG-END-OFFSET"), row.get("CONSUMER-ID")
            if old is None or not (cur_off or "").isdigit() or not (end or "").isdigit():
                continue
            old_off, old_end = old.get("CURRENT-OFFSET"), old.get("LOG-END-OFFSET")
            if not (old_off or "").isdigit() or not (old_end or "").isdigit():
                continue
            if consumer in (None, "-") or int(cur_off) != int(old_off) or int(end) <= int(old_end):
                continue
            host = (row.get("HOST") or "").lstrip("/")
            consumer_cid = by_ip.get(host)
            record = {
                "broker": broker,
                "group": key[0],
                "topic": key[1],
                "partition": key[2],
                "offset": int(cur_off),
                "log_end": [int(old_end), int(end)],
                "interval_s": interval,
                "consumer": consumer_cid,
            }
            findings.append(record)
            text = (
                f"Kafka consumer group {key[0]} is stalled at offset {cur_off} of topic {key[1]} partition {key[2]}: "
                f"its committed offset did not move in {interval}s while the log end advanced {old_end}->{end}"
            )
            target = snapshot.components.get(consumer_cid) if consumer_cid else snapshot.components.get(broker)
            if target is not None:
                target["signals"].append(text if consumer_cid else f"{text} (consumer host {host or 'unknown'})")
                if consumer_cid:
                    target["blocked_input"] = {
                        "source": "broker",
                        "system": "kafka",
                        **{k: record[k] for k in ("group", "topic", "partition", "offset")},
                    }
    if findings:
        snapshot.cluster["stalled_consumers"] = findings
    return findings


def broker_progress(
    snapshot: ClusterSnapshot,
    before: dict | None,
    interval: float = DEFAULT_BROKER_INTERVAL,
    skipped: dict | None = None,
) -> list[dict]:
    """Second broker sample and comparison. Takes the first sample now (and waits) when none was taken earlier."""
    if before is None:
        before = broker_samples(snapshot, skipped)
        if not before:
            return []
        time.sleep(interval)
    if not before:
        return []
    return apply_broker_progress(snapshot, before, broker_samples(snapshot, skipped))


# --------------------------------------------------------------------------- backend health probe

# (command, memory the command needs in the target container beyond its current usage)
_BACKEND_COMMANDS: dict[str, tuple[tuple[list[str], int], ...]] = {
    "redis": (
        (["valkey-cli", "-p", "{port}", "ping"], NATIVE_TOOL_BYTES),
        (["redis-cli", "-p", "{port}", "ping"], NATIVE_TOOL_BYTES),
    ),
    "postgresql": ((["pg_isready", "-h", "127.0.0.1", "-p", "{port}"], NATIVE_TOOL_BYTES),),
    "mongodb": (
        (["mongo", "--quiet", "--port", "{port}", "--eval", "db.runCommand({ping:1}).ok"], 4 * NATIVE_TOOL_BYTES),
        (["mongosh", "--quiet", "--port", "{port}", "--eval", "db.runCommand({ping:1}).ok"], SHELL_TOOL_BYTES),
    ),
    "mysql": ((["mysqladmin", "-h", "127.0.0.1", "-P", "{port}", "ping"], NATIVE_TOOL_BYTES),),
}
_MEANINGS = (
    (
        re.compile(
            r"(?i)NOAUTH|authentication required|requires authentication|Unauthorized|auth(?:entication)? failed"
        ),
        "the server requires authentication for this command",
    ),
    (re.compile(r"(?i)WRONGPASS|invalid password|access denied"), "the server rejected the credentials"),
    (re.compile(r"\bPONG\b"), "the server answers without authentication"),
    (re.compile(r"accepting connections"), "the server accepts connections"),
    (re.compile(r"rejecting connections"), "the server is rejecting connections"),
    (re.compile(r"\bLOADING\b"), "the server is still loading its dataset"),
    (re.compile(r"mysqld is alive"), "the server answers a ping"),
    (re.compile(r"(?m)^\s*1\s*$"), "the server answers a ping"),
    (
        re.compile(r"(?i)no response|connection refused|could not connect|connect: "),
        "the server does not answer on its own port",
    ),
)
_CREDENTIAL_ENV_RE = re.compile(r"(?i)(pass(word)?|pwd|auth|token|secret|credential)")


def client_credentials(comp: dict) -> str:
    names = [n for c in comp.get("containers") or [] for n in c.get("env_names") or [] if _CREDENTIAL_ENV_RE.search(n)]
    if names:
        return f"sets credential variables {', '.join(dict.fromkeys(names))}"
    secrets = (comp.get("config_refs") or {}).get("secrets") or []
    if secrets:
        return f"sets no credential variable by name and reads Secret(s) {', '.join(secrets)}"
    return "sets no credential variable and reads no Secret"


def backend_probe(snapshot: ClusterSnapshot, cid: str, client_cid: str | None = None) -> dict | None:
    """Run the role's health command inside the backend; None when the role has no command or none can run."""
    comp = snapshot.components.get(cid) or {}
    role = backend_role(comp)
    if role not in _BACKEND_COMMANDS:
        return None
    pod, container = _pick_pod(comp)
    if not pod:
        return None
    port = _port(comp, container, role)
    for template, need in _BACKEND_COMMANDS[role]:
        ok, why = headroom_ok(comp, pod, container, need)
        if not ok:
            logger.info("skipping %s in %s/%s: %s", template[0], comp["namespace"], pod, why)
            continue
        command = [part.format(port=port) for part in template]
        rc, text = run_in_pod(comp["namespace"], pod, container, command)
        if _missing_binary(rc, text) and "NOAUTH" not in text:
            continue
        meaning = next((m for rx, m in _MEANINGS if rx.search(text)), "unrecognised output")
        result = {"command": " ".join(command), "pod": pod, "output": trim(text, 200), "meaning": meaning}
        if client_cid and client_cid in snapshot.components:
            result["client"] = client_cid
            result["client_credentials"] = client_credentials(snapshot.components[client_cid])
        return result
    return None


def describe_probe_result(cid: str, result: dict) -> str:
    text = f"probe inside {cid}: `{result['command']}` returned '{result['output']}': {result['meaning']}"
    if result.get("client"):
        text += f"; client {result['client']} {result['client_credentials']}"
    return text
