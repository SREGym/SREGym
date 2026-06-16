"""Raw (operator-free) Cassandra ring application for multi-node bug reproduction.

The auto-generated ``GenericCustomBuildProblem`` harness deploys Cassandra through the
K8ssandra operator and expresses a bug as a single CQL ``reproducer`` string. Several
real Cassandra bugs cannot be expressed that way — they need a self-seeded multi-node
ring with per-pod JVM flags, per-replica gossip isolation, an in-pod killable
``CassandraDaemon`` process, or a node started with ``-Dcassandra.replace_address``.

``CassandraRawRingApplication`` deploys a STOCK ``cassandra:<version>`` image as a plain
headless ``Service`` + ``StatefulSet`` (``podManagementPolicy: OrderedReady``, seed =
``cass-0``, the rest self-join via ``CASSANDRA_SEEDS``) plus optional bare pods, and
exposes the per-pod ``kubectl exec`` primitives those reproductions need:

  * ``nodetool`` / ``cqlsh`` wrappers, gossip isolation (``disablegossip`` /
    ``enablegossip``), ``disablehandoff``, ``flush``, ``status`` parsing;
  * node-state waiting (``wait_node_state`` / ``wait_ring``);
  * bare-pod launch, in-pod process control (start/kill ``CassandraDaemon``), data-dir
    wipe, and ``system.log`` grep.

A loaded kind host makes Cassandra's failure detector misfire ("Not marking nodes down
due to local pause"); ``-Dcassandra.max_local_pause_in_ms`` is therefore added to every
node's JVM options so gossip-isolation conviction is deterministic.
"""

import logging
import shlex
import subprocess
import time

logger = logging.getLogger("all.application")

# Large value so a scheduling gap on a loaded kind host is not misread as a local JVM
# pause, which would otherwise suppress failure-detector conviction (FailureDetector.java
# "Not marking nodes down due to local pause") and make gossip isolation non-deterministic.
_MAX_LOCAL_PAUSE_MS = 600000


def _run(
    cmd: str, input: str | None = None, check: bool = False, timeout: int | None = None
) -> subprocess.CompletedProcess:
    """Run a shell command, inheriting the ambient KUBECONFIG."""
    result = subprocess.run(
        cmd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        input=input,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result


class CassandraRawRingApplication:
    """Deploy and drive a self-seeded raw Cassandra ring on the stock image."""

    name = "cassandra-raw-ring"

    def __init__(
        self,
        image: str,
        namespace: str,
        replicas: int = 2,
        cluster_name: str = "repro",
        num_tokens: int = 16,
        max_heap: str = "1024M",
        heap_newsize: str = "256M",
        jvm_extra_opts: str = "",
        hinted_handoff_enabled: bool = False,
        phi_convict_threshold: int = 5,
        service_name: str = "cass",
        statefulset_name: str = "cass",
        extra_pods: list[dict] | None = None,
        ready_timeout: int = 600,
    ):
        self.image = image
        self.namespace = namespace
        self.replicas = replicas
        self.cluster_name = cluster_name
        self.num_tokens = num_tokens
        self.max_heap = max_heap
        self.heap_newsize = heap_newsize
        self.jvm_extra_opts = jvm_extra_opts
        self.hinted_handoff_enabled = hinted_handoff_enabled
        self.phi_convict_threshold = phi_convict_threshold
        self.service_name = service_name
        self.statefulset_name = statefulset_name
        # Bare pods launched alongside the StatefulSet (e.g. a BOOT-parked joiner or a
        # tail -f /dev/null "target" whose CassandraDaemon is started/killed by hand).
        self.extra_pods = extra_pods or []
        self.ready_timeout = ready_timeout

    # ── DNS helpers ───────────────────────────────────────────────────────────

    def seed_dns(self) -> str:
        return f"cass-0.{self.service_name}.{self.namespace}.svc.cluster.local"

    def _jvm_opts(self) -> str:
        base = f"-Dcassandra.max_local_pause_in_ms={_MAX_LOCAL_PAUSE_MS}"
        return f"{base} {self.jvm_extra_opts}".strip()

    # ── Manifests ───────────────────────────────────────────────────────────────

    def _service_manifest(self) -> str:
        return f"""
apiVersion: v1
kind: Service
metadata:
  name: {self.service_name}
  namespace: {self.namespace}
  labels: {{ app: cass }}
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector: {{ app: cass }}
  ports:
    - {{ name: cql, port: 9042 }}
    - {{ name: intra, port: 7000 }}
"""

    def _node_command(self) -> str:
        hh = "true" if self.hinted_handoff_enabled else "false"
        return (
            "sed -ri 's/^(hinted_handoff_enabled:).*/\\1 " + hh + "/' /etc/cassandra/cassandra.yaml || true\n"
            "sed -ri 's/^# *(phi_convict_threshold:).*/\\1 " + str(self.phi_convict_threshold) + "/' "
            "/etc/cassandra/cassandra.yaml || true\n"
            "exec docker-entrypoint.sh cassandra -f\n"
        )

    def _statefulset_manifest(self) -> str:
        return f"""
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {self.statefulset_name}
  namespace: {self.namespace}
spec:
  serviceName: {self.service_name}
  podManagementPolicy: OrderedReady
  replicas: {self.replicas}
  selector: {{ matchLabels: {{ app: cass }} }}
  template:
    metadata:
      labels: {{ app: cass, role: ring }}
    spec:
      terminationGracePeriodSeconds: 20
      containers:
        - name: cassandra
          image: {self.image}
          imagePullPolicy: IfNotPresent
          ports:
            - {{ containerPort: 9042, name: cql }}
            - {{ containerPort: 7000, name: intra }}
          env:
            - {{ name: CASSANDRA_SEEDS, value: "{self.seed_dns()}" }}
            - {{ name: CASSANDRA_CLUSTER_NAME, value: "{self.cluster_name}" }}
            - {{ name: CASSANDRA_ENDPOINT_SNITCH, value: "GossipingPropertyFileSnitch" }}
            - {{ name: CASSANDRA_NUM_TOKENS, value: "{self.num_tokens}" }}
            - {{ name: MAX_HEAP_SIZE, value: "{self.max_heap}" }}
            - {{ name: HEAP_NEWSIZE, value: "{self.heap_newsize}" }}
            - {{ name: JVM_EXTRA_OPTS, value: "{self._jvm_opts()}" }}
          command: ["bash", "-c"]
          args:
            - |
{self._indent(self._node_command(), 14)}
          readinessProbe:
            exec: {{ command: ["bash", "-c", "cqlsh -e 'SELECT now() FROM system.local' >/dev/null 2>&1"] }}
            initialDelaySeconds: 25
            periodSeconds: 8
            failureThreshold: 40
          volumeMounts:
            - {{ name: data, mountPath: /var/lib/cassandra }}
      volumes:
        - {{ name: data, emptyDir: {{}} }}
"""

    @staticmethod
    def _indent(text: str, spaces: int) -> str:
        pad = " " * spaces
        return "\n".join(pad + line for line in text.splitlines())

    def bare_pod_manifest(
        self,
        pod_name: str,
        command: str | None = None,
        env: dict | None = None,
        set_seeds: bool = True,
    ) -> str:
        """Manifest for a bare pod attached to the ring's headless Service.

        ``command`` defaults to the normal node entrypoint. Pass
        ``tail -f /dev/null`` to create a "target" whose CassandraDaemon is launched
        and killed by hand. ``set_seeds=False`` omits CASSANDRA_SEEDS so the stock
        entrypoint self-seeds the pod (needed for the replace_address guard bugs).
        """
        env = dict(env or {})
        if set_seeds:
            env.setdefault("CASSANDRA_SEEDS", self.seed_dns())
        env.setdefault("CASSANDRA_CLUSTER_NAME", self.cluster_name)
        env.setdefault("CASSANDRA_ENDPOINT_SNITCH", "GossipingPropertyFileSnitch")
        env.setdefault("CASSANDRA_NUM_TOKENS", str(self.num_tokens))
        env.setdefault("MAX_HEAP_SIZE", self.max_heap)
        env.setdefault("HEAP_NEWSIZE", self.heap_newsize)
        env.setdefault("JVM_EXTRA_OPTS", self._jvm_opts())
        env_lines = "\n".join(f'        - {{ name: {k}, value: "{v}" }}' for k, v in env.items())
        body = command if command is not None else self._node_command()
        cmd_block = '      command: ["bash", "-c"]\n      args:\n        - |\n' + self._indent(body, 10)
        return f"""
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {self.namespace}
  labels: {{ app: cass, role: extra }}
spec:
  terminationGracePeriodSeconds: 10
  containers:
    - name: cassandra
      image: {self.image}
      imagePullPolicy: IfNotPresent
      ports:
        - {{ containerPort: 9042 }}
        - {{ containerPort: 7000 }}
      env:
{env_lines}
{cmd_block}
      volumeMounts:
        - {{ name: data, mountPath: /var/lib/cassandra }}
  volumes:
    - {{ name: data, emptyDir: {{}} }}
"""

    # ── Lifecycle (called by the Conductor) ─────────────────────────────────────

    def deploy(self):
        logger.info(f"[RawRing] Deploying {self.replicas}-node ring ({self.image}) in {self.namespace}")
        _run(f"kubectl create namespace {self.namespace} --dry-run=client -o yaml | kubectl apply -f -", check=True)
        _run("kubectl apply -f -", input=self._service_manifest(), check=True)
        _run("kubectl apply -f -", input=self._statefulset_manifest(), check=True)
        for spec in self.extra_pods:
            self.apply_bare_pod(**spec)
        self.wait_statefulset_ready()
        self.wait_ring(self.replicas)
        logger.info(f"[RawRing] Ring ready: {self.replicas}x UN in {self.namespace}")

    def start_workload(self):
        """No background workload — divergence/orchestration is driven by inject_fault."""

    def create_workload(self, **kwargs):
        pass

    def cleanup(self):
        logger.info(f"[RawRing] Cleaning up namespace {self.namespace}")
        _run(f"kubectl delete namespace {self.namespace} --ignore-not-found --wait=false")

    # ── Pod / exec primitives ───────────────────────────────────────────────────

    def apply_bare_pod(
        self, pod_name: str, command: str | None = None, env: dict | None = None, set_seeds: bool = True
    ):
        manifest = self.bare_pod_manifest(pod_name, command=command, env=env, set_seeds=set_seeds)
        _run("kubectl apply -f -", input=manifest, check=True)

    def launch_joiner(self, pod_name: str = "joiner", ring_delay_ms: int = 1800000, extra_opts: str = "") -> str:
        """Create a bootstrapping (non-seed) bare pod parked in BOOT/UJ via ring_delay_ms.

        The node announces BOOT to gossip then sleeps ``ring_delay_ms`` in the pending-range
        window ("JOINING: sleeping N ms for pending range setup"), so a coordinator observes
        it as ``UJ`` while writes are issued. Its CQL native transport stays down during
        bootstrap, so this is a bare pod (no cqlsh readiness probe), not a StatefulSet member.
        Returns the pod's IP once it is Running.
        """
        jvm = f"{self._jvm_opts()} -Dcassandra.ring_delay_ms={ring_delay_ms} {extra_opts}".strip()
        self.apply_bare_pod(pod_name, env={"JVM_EXTRA_OPTS": jvm})
        self.wait_pod_running(pod_name, timeout=240)
        return self.pod_ip(pod_name)

    def exec(self, pod: str, script: str, container: str = "cassandra", check: bool = False, timeout: int = 120) -> str:
        cmd = f"kubectl exec -n {self.namespace} {pod} -c {container} -- bash -lc {shlex.quote(script)}"
        try:
            res = _run(cmd, check=check, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f"[RawRing] exec timeout on {pod}: {script[:80]}")
            return ""
        return (res.stdout or "") + (res.stderr or "")

    def pod_ip(self, pod: str) -> str:
        return (
            _run(f"kubectl get pod -n {self.namespace} {pod} -o jsonpath='{{.status.podIP}}'").stdout.strip().strip("'")
        )

    def wait_pod_running(self, pod: str, timeout: int = 300) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            phase = (
                _run(f"kubectl get pod -n {self.namespace} {pod} -o jsonpath='{{.status.phase}}'")
                .stdout.strip()
                .strip("'")
            )
            if phase in ("Running", "Succeeded", "Failed"):
                return True
            time.sleep(3)
        return False

    def wait_pod_ready(self, pod: str, timeout: int = 300) -> bool:
        res = _run(f"kubectl wait --for=condition=Ready pod/{pod} -n {self.namespace} --timeout={timeout}s")
        return res.returncode == 0

    def wait_statefulset_ready(self):
        for i in range(self.replicas):
            pod = f"{self.statefulset_name}-{i}"
            if not self.wait_pod_ready(pod, timeout=self.ready_timeout):
                logger.warning(f"[RawRing] pod {pod} did not become Ready within {self.ready_timeout}s")

    # ── nodetool / cqlsh wrappers ───────────────────────────────────────────────

    def nodetool(self, pod: str, args: str, check: bool = False) -> str:
        return self.exec(pod, f"nodetool {args}", check=check)

    def cqlsh(self, pod: str, cql: str, timeout: int = 120) -> str:
        return self.exec(pod, f"cqlsh -e {shlex.quote(cql)}", timeout=timeout)

    def disablegossip(self, pod: str) -> str:
        return self.nodetool(pod, "disablegossip")

    def enablegossip(self, pod: str) -> str:
        return self.nodetool(pod, "enablegossip")

    def disablehandoff(self, pod: str) -> str:
        return self.nodetool(pod, "disablehandoff")

    def flush(self, pod: str, keyspace: str = "") -> str:
        return self.nodetool(pod, f"flush {keyspace}".strip())

    def node_state(self, observer_pod: str, target_ip: str) -> str:
        """Return the leading status token (UN/DN/UJ/...) the observer assigns to target_ip."""
        out = self.nodetool(observer_pod, "status")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == target_ip:
                return parts[0]
        return ""

    def wait_node_state(self, observer_pod: str, target_ip: str, want: str, timeout: int = 120) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.node_state(observer_pod, target_ip) == want:
                return True
            time.sleep(3)
        return False

    def wait_ring(self, expected_un: int, observer_pod: str = "cass-0", timeout: int = 480) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.nodetool(observer_pod, "status")
            un = sum(1 for ln in out.splitlines() if ln.strip().startswith("UN"))
            if un >= expected_un:
                return True
            time.sleep(5)
        logger.warning(f"[RawRing] wait_ring: only saw <{expected_un} UN within {timeout}s")
        return False

    # ── in-pod process control (killable CassandraDaemon) ───────────────────────

    def launch_daemon(self, pod: str, jvm_extra_opts: str = "", log_file: str = "/var/log/cassandra/launch.log") -> str:
        """Start CassandraDaemon as a background process inside a tail -f /dev/null pod."""
        extra = f"{self._jvm_opts()} {jvm_extra_opts}".strip()
        script = (
            f'export JVM_EXTRA_OPTS="{extra}"; '
            f"mkdir -p /var/log/cassandra; "
            f"setsid nohup docker-entrypoint.sh cassandra -f >{log_file} 2>&1 & echo launched pid=$!"
        )
        return self.exec(pod, script)

    def kill_daemon(self, pod: str) -> str:
        return self.exec(pod, "pkill -9 -f CassandraDaemon || true; sleep 1; echo killed")

    def daemon_running(self, pod: str) -> bool:
        out = self.exec(pod, "pgrep -f CassandraDaemon || true")
        return bool(out.strip())

    def wipe_data(self, pod: str) -> str:
        return self.exec(
            pod,
            "rm -rf /var/lib/cassandra/data /var/lib/cassandra/commitlog "
            "/var/lib/cassandra/hints /var/lib/cassandra/saved_caches; echo wiped",
        )

    # ── log inspection ──────────────────────────────────────────────────────────

    def system_log(self, pod: str, log_path: str = "/var/log/cassandra/system.log", tail: int = 4000) -> str:
        return self.exec(pod, f"tail -n {tail} {log_path} 2>/dev/null || true")

    def pod_logs(self, pod: str, previous: bool = False, tail: int = 4000) -> str:
        prev = "--previous" if previous else ""
        return _run(f"kubectl logs -n {self.namespace} {pod} -c cassandra {prev} --tail={tail}").stdout

    def pod_logs_all(self, pod: str, tail: int = 4000) -> str:
        """Concatenate the current and previous (crashed) container logs.

        For a crash-looping pod the discriminating startup line lives in whichever boot
        most recently reached it: the current attempt (if it is far enough along) or the
        last crashed attempt. Reading both makes the grep robust to restart timing.
        """
        cur = self.pod_logs(pod, previous=False, tail=tail)
        prev = self.pod_logs(pod, previous=True, tail=tail)
        return prev + "\n" + cur

    def grep_log(self, pod: str, pattern: str, source: str = "system_log", **kwargs) -> list[str]:
        text = self.system_log(pod) if source == "system_log" else self.pod_logs(pod, **kwargs)
        import re

        return [ln for ln in text.splitlines() if re.search(pattern, ln)]
