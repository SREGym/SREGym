"""Per-database build + deployment configuration.

Each DBBuildSpec describes four pipeline phases for one database type:

  1. Source   — where to clone source and how to map a version string to a git tag
  2. Build    — what Docker image to compile inside and what command to run
  3. Package  — where the compiled artifact lands and how to wrap it in a Docker image
  4. Deploy   — how to install the Kubernetes operator, generate the cluster CR manifest,
                and patch a live cluster to swap in a custom-built image (fault injection)

DB_REGISTRY maps a short name (e.g. "cassandra") to its DBBuildSpec so that
problem classes only need to declare ``db_name = "cassandra"``.
"""

import functools
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class DBBuildSpec:
    # ── Phase 1: Source ──────────────────────────────────────────────────────
    name: str
    repo_url: str
    # "owner/repo" on GitHub — used to match an issue URL to this spec.
    github_repo: str
    # Maps a bare version string to the git tag used in this repo.
    # e.g. "cassandra-{version}" → "cassandra-4.1.7"
    #      "v{version}"          → "v7.2.4"
    version_tag_pattern: str

    # ── Phase 2: Build ───────────────────────────────────────────────────────
    # Docker image that provides the right toolchain (JDK, GCC, Rust, …).
    build_image: str
    # Shell command run inside that image with the source tree as the working dir.
    build_cmd: str

    # ── Phase 3: Package ─────────────────────────────────────────────────────
    # Glob relative to the source root that matches the compiled artifact.
    artifact_glob: str
    # Base Docker image to extend.  May contain {version} which is substituted
    # with the bare version string (e.g. "4.1.7").
    base_image: str
    # Absolute path inside the base image where the artifact is copied.
    artifact_dest: str

    # ── Phase 4: Deploy ──────────────────────────────────────────────────────
    # Helm details for the Kubernetes operator that manages this database.
    operator_helm_repo: str
    operator_helm_repo_url: str
    operator_chart: str
    operator_namespace: str

    # Default cluster name used when deploying (can be overridden per problem).
    default_cluster_name: str

    # Lowercase singular CR kind as kubectl expects it
    # (e.g. "k8ssandracluster", "tidbcluster").
    cr_kind: str

    # Generates the Kubernetes CR manifest YAML for this database.
    # Called as: cluster_manifest_fn(cluster_name, namespace, version, custom_image)
    # custom_image is None for a stock deploy; set to an image tag for a buggy deploy.
    # Unused (and may be None) when helm_deploy_chart=True — the Helm install IS the deploy.
    cluster_manifest_fn: Callable[[str, str, str, str | None], str] | None

    # Returns the JSON merge-patch dict that swaps the running cluster's image.
    # Called as: image_patch_fn(cluster_name, namespace, new_image) → dict
    # Unused when helm_deploy_chart=True — the StatefulSet is patched directly instead.
    image_patch_fn: Callable[[str, str, str], dict]

    # Optional operator prerequisites (e.g. cert-manager for K8ssandra).
    # Called once before the Helm operator install with no arguments.
    prereqs_fn: Callable[[], None] | None = field(default=None)

    # Jira project key for this database (e.g. "CASSANDRA", "ZOOKEEPER").
    # Set this so JiraIssueParser can map an issue URL to this spec.
    jira_project: str | None = field(default=None)

    # Runs a reproducer (SQL/shell/script string) against the live cluster once.
    # Called as: run_reproducer_fn(cluster_name, namespace, reproducer)
    run_reproducer_fn: Callable[[str, str, str], None] | None = field(default=None)

    # Returns a Kubernetes manifest (ConfigMap + Deployment) that continuously
    # runs the reproducer on the cluster so the bug stays observable.
    # Called as: reproducer_workload_fn(cluster_name, namespace, reproducer, expected_output) → str
    # expected_output: the BUGGY value for wrong-result bugs (the probe greps for it, so
    # Ready = bug present); None for error/crash bugs. See ReproducerPodMitigationOracle.
    reproducer_workload_fn: Callable[[str, str, str, str | None], str] | None = field(default=None)

    # Extra --set / --values flags appended to the Helm operator install command.
    operator_extra_helm_args: str = field(default="")

    # Skip source compile and produce a custom image that just re-tags the stock
    # base image.  Use this when the source tree cannot be compiled standalone
    # (e.g. MongoDB 8.0+ public repo unconditionally references a private
    # enterprise modules repo) but the bug is already present in the stock
    # binary.  build_cmd / artifact_glob / artifact_dest are ignored in this mode.
    prebuilt_from_stock: bool = field(default=False)

    # The Helm release IS the cluster (no separate operator + CR).  Used for
    # CockroachDB, where cockroach-operator was sunset and the supported path
    # is the direct cockroachdb/cockroachdb chart which renders a StatefulSet
    # with no CRD.  When True:
    #   • cluster_manifest_fn / image_patch_fn / cr_kind are unused (may be None/"")
    #   • operator_namespace doubles as the cluster namespace
    #   • image swaps go to the StatefulSet directly (via _operator_override path)
    helm_deploy_chart: bool = field(default=False)

    # Optional version-aware override for the deploy/base image. When set it
    # fully replaces the `base_image.format(version=…)` default — used by
    # Cassandra, whose published image suffix depends on the version
    # (`-ubi8` for 3.11/4.x, `-ubi` for 5.0.x). Default None → template format.
    base_image_resolver: Callable[[str], str] | None = field(default=None)

    # Optional override for the `app.kubernetes.io/instance` label value the
    # readiness wait selects on. Default None → the cluster name (correct for
    # TiDB / CockroachDB / Mongo). K8ssandra prefixes cassandra pods with
    # `cassandra-`, so Cassandra maps cluster_name → f"cassandra-{cluster_name}".
    ready_instance_label_fn: Callable[[str], str] | None = field(default=None)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def git_ref(self, version: str) -> str:
        """Convert a bare version string to the git tag for this database."""
        return self.version_tag_pattern.format(version=version)

    def resolved_base_image(self, version: str) -> str:
        """Resolve the base/deploy image for a version.

        Uses base_image_resolver when provided (version-aware), else substitutes
        {version} into the base_image template.
        """
        if self.base_image_resolver is not None:
            return self.base_image_resolver(version)
        return self.base_image.format(version=version)

    def resolved_ready_instance_label(self, cluster_name: str) -> str:
        """The app.kubernetes.io/instance value the readiness wait selects on."""
        if self.ready_instance_label_fn is not None:
            return self.ready_instance_label_fn(cluster_name)
        return cluster_name

    def resolved_artifact_dest(self, version: str) -> str:
        """Substitute {version} in artifact_dest (some images have version in their paths)."""
        return self.artifact_dest.format(version=version)


# ── Per-DB functions ──────────────────────────────────────────────────────────

def _cassandra_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {"spec": {"cassandra": {"serverImage": image}}}


@functools.cache
def _cass_mgmt_tag_exists(image: str) -> bool:
    """True if a k8ssandra/cass-management-api tag exists on Docker Hub.

    Cached so a single problem's deploy/inject (which resolves the base image a
    handful of times) probes each tag at most once. Returns False when Docker or
    the network is unavailable so the caller can fall back to a static heuristic.
    """
    try:
        return (
            subprocess.run(
                f"docker manifest inspect {image}",
                shell=True,
                capture_output=True,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _cassandra_base_image(version: str) -> str:
    """Resolve the k8ssandra/cass-management-api base image for a Cassandra version.

    The published suffix differs across release eras: 3.11.x / 4.0.x / 4.1.<=9 use
    ``-ubi8``; 4.1.>=10 and 5.0.x dropped ``-ubi8`` and publish ``-ubi`` (plus a bare
    tag). A static ``major >= 5`` heuristic therefore misses 4.1.10/4.1.11, whose
    ``-ubi8`` tags do not exist (the resolver would return a 404 image and every
    build/deploy for those versions fails). Probe Docker Hub and return the first
    suffix that actually exists, preferring the era-typical suffix; fall back to the
    static heuristic when nothing can be probed (offline)."""
    try:
        major = int(version.split(".")[0])
    except (ValueError, IndexError):
        major = 0
    repo = "k8ssandra/cass-management-api"
    preferred = ["ubi", "ubi8"] if major >= 5 else ["ubi8", "ubi"]
    for suffix in preferred:
        candidate = f"{repo}:{version}-{suffix}"
        if _cass_mgmt_tag_exists(candidate):
            return candidate
    bare = f"{repo}:{version}"
    if _cass_mgmt_tag_exists(bare):
        return bare
    suffix = "ubi" if major >= 5 else "ubi8"
    return f"{repo}:{version}-{suffix}"


def _cassandra_ready_instance_label(cluster_name: str) -> str:
    """K8ssandra/cass-operator labels managed pods with
    app.kubernetes.io/instance=cassandra-<clusterName>, not <clusterName>."""
    return f"cassandra-{cluster_name}"


def _cassandra_needs_mcac(version: str) -> bool:
    """The K8ssandra operator's default (new) metrics endpoint exists only since
    Cassandra 3.11.13 / 4.0.4. For older servers the operator errors with
    'MCAC cannot be disabled' and never creates a StatefulSet, so we must keep
    MCAC explicitly enabled."""
    try:
        parts = [int(x) for x in version.split(".")[:3]]
    except (ValueError, IndexError):
        return False
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    if major == 3 and (minor < 11 or (minor == 11 and patch < 13)):
        return True
    return major == 4 and minor == 0 and patch < 4


def _cassandra_cluster_manifest(
    cluster_name: str,
    namespace: str,
    version: str,
    custom_image: str | None,
) -> str:
    server_image = f'\n    serverImage: "{custom_image}"' if custom_image else ""
    # Old servers (<3.11.13 / <4.0.4) cannot use the operator's new metrics
    # endpoint; force MCAC on so the operator stops trying to disable it.
    telemetry = "\n    telemetry:\n      mcac:\n        enabled: true" if _cassandra_needs_mcac(version) else ""
    return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {cluster_name}
  namespace: {namespace}
spec:
  cassandra:
    serverVersion: "{version}"{server_image}{telemetry}
    datacenters:
      - metadata:
          name: dc1
        size: 3
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: openebs-hostpath
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 5Gi
        resources:
          requests:
            memory: 1Gi
            cpu: 500m
          limits:
            memory: 2Gi
            cpu: "1"
        config:
          jvmOptions:
            heapSize: 512M
"""


def _ensure_cert_manager() -> None:
    """Install cert-manager if not already present (required by K8ssandra webhooks)."""
    import subprocess
    import logging
    log = logging.getLogger(__name__)

    result = subprocess.run(
        "kubectl get namespace cert-manager --ignore-not-found",
        shell=True, capture_output=True, text=True,
    )
    if "cert-manager" in result.stdout:
        return

    log.info("Installing cert-manager (required by K8ssandra operator)...")
    subprocess.run(
        "kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml",
        shell=True, check=True,
    )
    subprocess.run(
        "kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s",
        shell=True, check=True,
    )
    subprocess.run(
        "kubectl wait pod --all -n cert-manager --for=condition=Ready --timeout=120s",
        shell=True, check=True,
    )
    log.info("cert-manager ready")


def _tidb_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {"spec": {"tidb": {"image": image}}}


def _tidb_cluster_manifest(
    cluster_name: str,
    namespace: str,
    version: str,
    custom_image: str | None,
) -> str:
    tidb_image = f'\n    image: "{custom_image}"' if custom_image else ""
    return f"""\
apiVersion: pingcap.com/v1alpha1
kind: TidbCluster
metadata:
  name: {cluster_name}
  namespace: {namespace}
spec:
  version: "v{version}"
  timezone: UTC
  pvReclaimPolicy: Delete
  enableDynamicConfiguration: true
  configUpdateStrategy: RollingUpdate
  discovery: {{}}
  helper:
    image: alpine:3.16.0
  pd:
    baseImage: pingcap/pd
    maxFailoverCount: 0
    replicas: 1
    storageClassName: openebs-hostpath
    requests:
      storage: 1Gi
    config: {{}}
  tikv:
    baseImage: pingcap/tikv
    maxFailoverCount: 0
    replicas: 1
    evictLeaderTimeout: 1m
    storageClassName: openebs-hostpath
    requests:
      storage: 1Gi
    config:
      storage:
        reserve-space: 0MB
      rocksdb:
        max-open-files: 256
      raftdb:
        max-open-files: 256
  tidb:
    baseImage: pingcap/tidb
    maxFailoverCount: 0
    replicas: 1{tidb_image}
    service:
      type: ClusterIP
    config: {{}}
"""


def _ensure_tidb_crds() -> None:
    """Install TiDB CRDs if not already present."""
    import subprocess
    import logging
    log = logging.getLogger(__name__)

    result = subprocess.run(
        "kubectl get crd tidbclusters.pingcap.com --ignore-not-found",
        shell=True, capture_output=True, text=True,
    )
    if "tidbclusters" in result.stdout:
        return

    log.info("Installing TiDB CRDs...")
    subprocess.run(
        "kubectl apply --server-side -f https://raw.githubusercontent.com/pingcap/tidb-operator/v1.6.0/manifests/crd.yaml",
        shell=True, check=True,
    )
    log.info("TiDB CRDs installed")


# ── Reproducer runners ───────────────────────────────────────────────────────

def _cassandra_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import base64
    import logging
    import shlex
    import subprocess
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-dc1-service.{namespace}.svc.cluster.local"
    pod = "cassandra-cql-client"

    # The K8ssandra operator enables PasswordAuthenticator by default and creates a
    # `<cluster>-superuser` secret. Fetch it so cqlsh can authenticate — otherwise the
    # setup/reproducer CQL fails with AuthenticationFailed and the schema is never created
    # (so any later flush/SELECT can't reproduce the bug). Falls back to no-auth when the
    # secret is absent (auth disabled / non-K8ssandra deploy).
    secret = f"{cluster_name}-superuser"

    def _secret_val(key: str) -> str:
        r = subprocess.run(
            f"kubectl get secret {secret} -n {namespace} -o jsonpath='{{.data.{key}}}'",
            shell=True, capture_output=True, text=True,
        )
        raw = r.stdout.strip()
        if r.returncode != 0 or not raw:
            return ""
        try:
            return base64.b64decode(raw).decode()
        except Exception:
            return ""

    user = _secret_val("username")
    pw = _secret_val("password")
    auth = f"-u {shlex.quote(user)} -p {shlex.quote(pw)} " if user and pw else ""

    log.info("[Reproducer] Running Cassandra CQL reproducer" + (" (authenticated)" if auth else ""))
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=cassandra:4.1 --restart=Never -n {namespace} -- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- cqlsh {auth}{svc}",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0:
            log.info(f"[Reproducer] Query completed: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] cqlsh exited {result.returncode} (may be expected): {stderr[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


def _tidb_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import subprocess
    import logging
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-tidb.{namespace}.svc.cluster.local"
    pod = "tidb-sql-client"

    # Wrap reproducer in a fresh temp database to avoid table-exists errors and
    # DROP DATABASE IF EXISTS raising ERROR 1008 on TiDB when DB doesn't exist.
    db_name = "sregym_oneshot"
    reproducer = (
        f"CREATE DATABASE IF NOT EXISTS `{db_name}`;\n"
        f"USE `{db_name}`;\n"
        + reproducer
    )

    log.info("[Reproducer] Running TiDB SQL reproducer")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=mysql:8.0 --restart=Never -n {namespace} -- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- "
            f"mysql -h {svc} -P 4000 -u root --connect-timeout=15",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0:
            log.info(f"[Reproducer] Query completed: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] mysql exited {result.returncode} (may be expected): {stderr[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


# ── Continuous reproducer workloads ──────────────────────────────────────────

def _strip_sql_db_setup(sql: str) -> str:
    """Remove CREATE DATABASE / USE statements from a SQL reproducer.

    The continuous workload loop already prepends its own per-iteration DB
    creation and USE, so any such lines in the raw reproducer would either
    collide (database exists on iteration 2+) or silently switch to the wrong
    database.
    """
    import re
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if re.match(r"(?i)create\s+database\b", stripped):
            continue
        if re.match(r"(?i)use\s+\S", stripped):
            continue
        lines.append(line)
    # Drop leading blank lines left by the removed statements.
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def _workload_manifest(
    cluster_name: str,
    namespace: str,
    client_image: str,
    loop_cmd: str,
    script_content: str,
    script_filename: str = "run.script",
    probe_script: str | None = None,
    env_yaml: str = "",
) -> str:
    """Build a ConfigMap + Deployment manifest that runs script_content in a loop.

    probe_script: shell script stored as probe.sh in the ConfigMap and executed
    by the readiness probe.  It should exit 0 when the DB is reachable (even if
    the reproducer query fails with the expected bug error) and exit 1 when the
    DB cannot be reached.  Always invoked as /bin/sh /scripts/probe.sh so the
    probe command itself is fully generic.
    """
    indented = "\n".join("    " + l for l in script_content.splitlines())

    probe_entry = ""
    probe_yaml = ""
    if probe_script:
        probe_indented = "\n".join("    " + l for l in probe_script.splitlines())
        probe_entry = f"\n  probe.sh: |\n{probe_indented}"
        probe_yaml = """
        readinessProbe:
          exec:
            command:
              - /bin/sh
              - /scripts/probe.sh
          initialDelaySeconds: 15
          periodSeconds: 10
          failureThreshold: 3"""

    return f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {cluster_name}-reproducer
  namespace: {namespace}
data:
  {script_filename}: |
{indented}{probe_entry}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {cluster_name}-reproducer
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: reproducer
    app.kubernetes.io/instance: {cluster_name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {cluster_name}-reproducer
  template:
    metadata:
      labels:
        app: {cluster_name}-reproducer
    spec:
      volumes:
      - name: script
        configMap:
          name: {cluster_name}-reproducer
      containers:
      - name: reproducer
        image: {client_image}{env_yaml}
        volumeMounts:
        - name: script
          mountPath: /scripts
        command: ["/bin/sh", "-c"]
        args:
        - |
          {loop_cmd}{probe_yaml}
      restartPolicy: Always

"""


def _tidb_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None = None
) -> str:
    reproducer = _strip_sql_db_setup(reproducer)
    # run.sql holds the raw reproducer; the shell loop prepends DB setup so we
    # never run DROP DATABASE (which raises ERROR 1008 on TiDB even with IF EXISTS).
    # A timestamped DB name avoids table-already-exists errors between iterations.
    svc = f"{cluster_name}-tidb.{namespace}.svc.cluster.local"
    mysql = f"mysql -h {svc} -P 4000 -u root --connect-timeout=15"
    loop_cmd = (
        "while true; do "
                'DB=sregym_$(date +%s); '
        # printf preamble → cat script → pipe to mysql; no backtick quoting needed
        # for sregym_<timestamp> identifiers
        f'out=$(printf "SET GLOBAL tidb_mem_quota_query=0;\\nSET SESSION max_execution_time=15000;\\nCREATE DATABASE $DB;\\nUSE $DB;\\n" | cat - /scripts/run.sql | timeout 30 {mysql} --table 2>&1); '
        'rc=$?; if [ -n "$out" ]; then echo "$out"; else echo "(empty result set)"; fi; '
        f'mysql -h {svc} -P 4000 -u root --connect-timeout=5 -e "DROP DATABASE $DB" > /dev/null 2>&1 || true; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            "DB=sregym_probe\n"
            f'mysql -h {svc} -P 4000 -u root --connect-timeout=5 -e "CREATE DATABASE IF NOT EXISTS $DB" > /dev/null 2>&1\n'
            f'out=$(printf "USE $DB;\\n" | cat - /scripts/run.sql | {mysql} --batch --skip-column-names 2>/dev/null)\n'
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        probe_script = (
            "#!/bin/sh\n"
            "DB=sregym_probe_$(date +%s)\n"
            f'printf "CREATE DATABASE $DB;\\nUSE $DB;\\n" | cat - /scripts/run.sql | {mysql} > /dev/null 2>&1\n'
            "rc=$?\n"
            f'mysql -h {svc} -P 4000 -u root --connect-timeout=5 -e "DROP DATABASE $DB" > /dev/null 2>&1 || true\n'
            "exit $rc\n"
        )
    return _workload_manifest(cluster_name, namespace, "mysql:8.0", loop_cmd, reproducer, "run.sql", probe_script)


def _mongodb_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {
        "spec": {
            "statefulSet": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "mongod", "image": image}]
                        }
                    }
                }
            }
        }
    }


def _mongodb_cluster_manifest(
    cluster_name: str,
    namespace: str,
    version: str,
    custom_image: str | None,
) -> str:
    custom_image_override = (
        f"\n        spec:\n          containers:\n          - name: mongod\n            image: \"{custom_image}\""
        if custom_image else ""
    )
    return f"""\
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-admin-password
  namespace: {namespace}
type: Opaque
stringData:
  password: sregym-test-pass
---
apiVersion: mongodbcommunity.mongodb.com/v1
kind: MongoDBCommunity
metadata:
  name: {cluster_name}
  namespace: {namespace}
spec:
  members: 1
  type: ReplicaSet
  version: "{version}"
  security:
    authentication:
      modes: ["SCRAM"]
  users:
    - name: admin
      db: admin
      passwordSecretRef:
        name: mongodb-admin-password
      roles:
        - name: clusterAdmin
          db: admin
        - name: userAdminAnyDatabase
          db: admin
        - name: readWriteAnyDatabase
          db: admin
      scramCredentialsSecretName: mongodb-admin-scram
  statefulSet:
    spec:
      template:
        metadata:
          labels:
            app.kubernetes.io/instance: {cluster_name}{custom_image_override}
"""


def _mongodb_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import subprocess
    import logging
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-svc.{namespace}.svc.cluster.local"
    conn = (
        f"mongodb://admin:sregym-test-pass@{svc}:27017/admin"
        f"?authSource=admin&replicaSet={cluster_name}"
    )
    pod = "mongodb-mongosh-client"

    log.info("[Reproducer] Running MongoDB reproducer")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=mongo:6 --restart=Never -n {namespace} -- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- mongosh '{conn}' --quiet",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            log.info(f"[Reproducer] Output: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] mongosh exited {result.returncode}: {result.stderr.strip()[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


def _mongodb_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None
) -> str:
    svc = f"{cluster_name}-svc.{namespace}.svc.cluster.local"
    conn = (
        f"mongodb://admin:sregym-test-pass@{svc}:27017/admin"
        f"?authSource=admin&replicaSet={cluster_name}"
    )
    loop_cmd = (
        "while true; do "
                f"out=$(mongosh '{conn}' --quiet < /scripts/run.js 2>&1); "
        'rc=$?; [ -n "$out" ] && echo "$out"; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            f"out=$(mongosh '{conn}' --quiet < /scripts/run.js 2>/dev/null)\n"
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        probe_script = (
            "#!/bin/sh\n"
            f"mongosh '{conn}' --quiet < /scripts/run.js > /dev/null 2>&1\n"
        )
    return _workload_manifest(cluster_name, namespace, "mongo:6", loop_cmd, reproducer, "run.js", probe_script)


# Unused under helm_deploy_chart (no CR to patch) but kept to satisfy the
# required-field schema and in case a future deploy mode routes through it.
def _cockroachdb_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {"spec": {"template": {"spec": {"containers": [{"name": "db", "image": image}]}}}}


def _cockroachdb_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import subprocess
    import logging
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-public.{namespace}.svc.cluster.local"
    pod = "cockroach-sql-client"

    # Wrap in a fresh temp DB so repeated runs can't collide on CREATE TABLE.
    db_name = "sregym_oneshot"
    reproducer = (
        f"CREATE DATABASE IF NOT EXISTS {db_name};\n"
        f"USE {db_name};\n"
        + reproducer
    )

    log.info("[Reproducer] Running CockroachDB SQL reproducer")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        # --command overrides the entrypoint so sleep runs via /bin/sh, not cockroach.
        subprocess.run(
            f"kubectl run {pod} --image=cockroachdb/cockroach:v24.1.4 "
            f"--restart=Never -n {namespace} --command -- /bin/sh -c 'sleep 3600'",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- "
            f"cockroach sql --insecure --host={svc} --port=26257",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0:
            log.info(f"[Reproducer] Query completed: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] cockroach sql exited {result.returncode} (may be expected): {stderr[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


def _cockroachdb_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None = None
) -> str:
    reproducer = _strip_sql_db_setup(reproducer)
    # Same shape as the TiDB workload: per-iteration temp DB avoids table-exists
    # errors and CockroachDB's MVCC-aware DROP DATABASE is cheap enough to use
    # between iterations.
    svc = f"{cluster_name}-public.{namespace}.svc.cluster.local"
    cockroach = f"cockroach sql --insecure --host={svc} --port=26257"
    loop_cmd = (
        "while true; do "
        'DB=sregym_$(date +%s); '
        f'out=$(printf "CREATE DATABASE $DB;\\nUSE $DB;\\n" | cat - /scripts/run.sql | timeout 30 {cockroach} 2>&1); '
        'rc=$?; if [ -n "$out" ]; then echo "$out"; else echo "(empty result set)"; fi; '
        f'{cockroach} -e "DROP DATABASE $DB CASCADE" > /dev/null 2>&1 || true; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            "DB=sregym_probe\n"
            f'{cockroach} -e "CREATE DATABASE IF NOT EXISTS $DB" > /dev/null 2>&1\n'
            f'out=$(printf "USE $DB;\\n" | cat - /scripts/run.sql | {cockroach} --format=tsv 2>/dev/null)\n'
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        probe_script = (
            "#!/bin/sh\n"
            "DB=sregym_probe_$(date +%s)\n"
            f'printf "CREATE DATABASE $DB;\\nUSE $DB;\\n" | cat - /scripts/run.sql | {cockroach} > /dev/null 2>&1\n'
            "rc=$?\n"
            f'{cockroach} -e "DROP DATABASE $DB CASCADE" > /dev/null 2>&1 || true\n'
            "exit $rc\n"
        )
    return _workload_manifest(
        cluster_name, namespace, "cockroachdb/cockroach:v24.1.4",
        loop_cmd, reproducer, "run.sql", probe_script,
    )


def _cassandra_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None = None
) -> str:
    svc = f"{cluster_name}-dc1-service.{namespace}.svc.cluster.local"
    # The K8ssandra operator enables PasswordAuthenticator by default and creates
    # a `<cluster>-superuser` secret. Inject it so cqlsh can authenticate —
    # otherwise every query fails with AuthenticationFailed and the readiness
    # signal reflects auth, not the bug.
    secret = f"{cluster_name}-superuser"
    env_yaml = (
        "\n        env:"
        "\n        - name: CASS_USER"
        "\n          valueFrom:"
        "\n            secretKeyRef:"
        f"\n              name: {secret}"
        "\n              key: username"
        "\n        - name: CASS_PASS"
        "\n          valueFrom:"
        "\n            secretKeyRef:"
        f"\n              name: {secret}"
        "\n              key: password"
    )
    auth = '-u "$CASS_USER" -p "$CASS_PASS"'
    loop_cmd = (
        "while true; do "
        f"out=$(cqlsh {auth} {svc} < /scripts/run.cql 2>&1); "
        'rc=$?; [ -n "$out" ] && echo "$out"; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            f"out=$(cqlsh {auth} {svc} --no-color < /scripts/run.cql 2>/dev/null)\n"
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        # Error/crash bug: Ready = query runs without error (bug fixed).
        # Not Ready = query errors (bug active) or DB unreachable.
        probe_script = (
            "#!/bin/sh\n"
            f"cqlsh {auth} {svc} < /scripts/run.cql > /dev/null 2>&1\n"
        )
    return _workload_manifest(
        cluster_name, namespace, "cassandra:4.1", loop_cmd, reproducer,
        "run.cql", probe_script, env_yaml=env_yaml,
    )


# ── etcd helpers ─────────────────────────────────────────────────────────────

def _etcd_image_patch(_cluster: str, _ns: str, image: str) -> dict:
    return {"spec": {"template": {"spec": {"containers": [{"name": "etcd", "image": image}]}}}}


def _etcd_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import logging
    import subprocess
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}.{namespace}.svc.cluster.local"
    endpoint = f"http://{svc}:2379"
    pod = "etcd-repro-client"

    log.info("[Reproducer] Running etcd reproducer via client pod")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=alpine:3.20 "
            f"--restart=Never -n {namespace} "
            f"-- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl exec {pod} -n {namespace} -- "
            "sh -c 'wget -qO /tmp/etcd.tgz https://github.com/etcd-io/etcd/releases/download/v3.5.17/etcd-v3.5.17-linux-amd64.tar.gz && "
            "tar xzf /tmp/etcd.tgz -C /tmp && cp /tmp/etcd-v3.5.17-linux-amd64/etcdctl /usr/local/bin/ && rm -rf /tmp/etcd*'",
            shell=True, check=True, capture_output=True, timeout=120,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- "
            f"sh -c 'export ETCDCTL_ENDPOINTS={endpoint}; sh'",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            log.info(f"[Reproducer] Completed: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] Exited {result.returncode} (may be expected): {result.stderr.strip()[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


def _etcd_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None = None
) -> str:
    svc = f"{cluster_name}.{namespace}.svc.cluster.local"
    endpoint = f"http://{svc}:2379"
    install = (
        "if ! command -v etcdctl >/dev/null 2>&1; then "
        "wget -qO /tmp/etcd.tgz https://github.com/etcd-io/etcd/releases/download/v3.5.17/etcd-v3.5.17-linux-amd64.tar.gz && "
        "tar xzf /tmp/etcd.tgz -C /tmp && cp /tmp/etcd-v3.5.17-linux-amd64/etcdctl /usr/local/bin/ && "
        "rm -rf /tmp/etcd*; fi; "
    )
    loop_cmd = (
        f"{install}"
        f"export ETCDCTL_ENDPOINTS={endpoint}; "
        "while true; do "
        "sh /scripts/run.sh > /tmp/repro.out 2>&1; "
        "echo $? > /tmp/probe_rc; "
        "cat /tmp/repro.out; "
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            "[ -f /tmp/repro.out ] || exit 1\n"
            f"grep -qF '{safe}' /tmp/repro.out && exit 0\n"
            "exit 1\n"
        )
    else:
        probe_script = (
            "#!/bin/sh\n"
            "[ -f /tmp/probe_rc ] || exit 1\n"
            '[ "$(cat /tmp/probe_rc)" = "0" ] && exit 0\n'
            "exit 1\n"
        )
    return _workload_manifest(
        cluster_name, namespace, "alpine:3.20",
        loop_cmd, reproducer, "run.sh", probe_script,
    )


# ── Registry ─────────────────────────────────────────────────────────────────

DB_REGISTRY: dict[str, DBBuildSpec] = {
    "cassandra": DBBuildSpec(
        name="cassandra",
        repo_url="https://github.com/apache/cassandra",
        github_repo="apache/cassandra",
        version_tag_pattern="cassandra-{version}",
        build_image="eclipse-temurin:11",
        build_cmd=(
            "apt-get update -qq && apt-get install -y -q ant && "
            "ant jar -Duse.jdk11=true"
        ),
        artifact_glob="build/apache-cassandra-*.jar",
        base_image="k8ssandra/cass-management-api:{version}-ubi8",
        base_image_resolver=_cassandra_base_image,
        ready_instance_label_fn=_cassandra_ready_instance_label,
        artifact_dest="/opt/cassandra/lib/",
        operator_helm_repo="k8ssandra",
        operator_helm_repo_url="https://helm.k8ssandra.io/stable",
        operator_chart="k8ssandra/k8ssandra-operator",
        operator_namespace="k8ssandra-operator",
        default_cluster_name="sregym-cassandra",
        cr_kind="k8ssandracluster",
        cluster_manifest_fn=_cassandra_cluster_manifest,
        image_patch_fn=_cassandra_image_patch,
        prereqs_fn=_ensure_cert_manager,
        jira_project="CASSANDRA",
        run_reproducer_fn=_cassandra_run_reproducer,
        reproducer_workload_fn=_cassandra_reproducer_workload,
    ),
    "tidb": DBBuildSpec(
        name="tidb",
        repo_url="https://github.com/pingcap/tidb",
        github_repo="pingcap/tidb",
        version_tag_pattern="v{version}",
        build_image="golang:1.23",
        build_cmd="GOFLAGS=-buildvcs=false make server || make tidb-server || make build",
        artifact_glob="bin/tidb-server*",
        base_image="pingcap/tidb:v{version}",
        artifact_dest="/tidb-server",
        operator_helm_repo="pingcap",
        operator_helm_repo_url="https://charts.pingcap.org",
        operator_chart="pingcap/tidb-operator",
        operator_namespace="tidb-admin",
        default_cluster_name="sregym-tidb",
        cr_kind="tidbcluster",
        cluster_manifest_fn=_tidb_cluster_manifest,
        image_patch_fn=_tidb_image_patch,
        prereqs_fn=_ensure_tidb_crds,
        jira_project=None,
        run_reproducer_fn=_tidb_run_reproducer,
        reproducer_workload_fn=_tidb_reproducer_workload,
        # Pin chart to match the CRD manifest version — mixing CRD v1.6.0 with
        # chart v1.6.5 leaves compactbackups.pingcap.com missing, which stalls
        # the controller reconciliation loop and prevents any pods from starting.
        # Disable admission webhook and scheduler (both need cert infra on kind).
        operator_extra_helm_args=(
            "--version v1.6.0 "
            "--set admissionWebhook.create=false "
            "--set scheduler.create=false"
        ),
    ),
    "mongodb": DBBuildSpec(
        name="mongodb",
        repo_url="https://github.com/mongodb/mongo",
        github_repo="mongodb/mongo",
        # MongoDB release tags use "r" prefix: r7.0.5, r6.0.10, etc.
        version_tag_pattern="r{version}",
        # build_image / build_cmd / artifact_glob / artifact_dest are not used
        # when prebuilt_from_stock=True.  Kept as placeholders for the schema.
        build_image="ubuntu:22.04",
        build_cmd="true",
        artifact_glob="UNUSED",
        base_image="mongodb/mongodb-community-server:{version}-ubi8",
        artifact_dest="/usr/bin/mongod",
        # The mongo public source at 8.0+ cannot be built standalone:
        #   • src/BUILD.bazel:10 (core_headers_library) unconditionally references
        #     //src/mongo/db/modules/enterprise/... which lives in a private repo.
        #   • .bazelrc pins build_enterprise=True in every profile.
        #   • SCons is mid-migration and fails on murmurhash3/s2/snappy/tcmalloc.
        # Community jira bugs like SERVER-110803 exist in the published stock
        # image (e.g. mongodb/mongodb-community-server:8.0.13-ubi8), so the
        # "custom" image can simply wrap the stock one — the buggy binary is
        # already there.  The source tree is still cloned for static analysis
        # by diagnosis oracles.
        prebuilt_from_stock=True,
        operator_helm_repo="mongodb",
        operator_helm_repo_url="https://mongodb.github.io/helm-charts",
        operator_chart="mongodb/community-operator",
        operator_namespace="mongodb-operator",
        default_cluster_name="sregym-mongodb",
        cr_kind="mongodbcommunity",
        cluster_manifest_fn=_mongodb_cluster_manifest,
        image_patch_fn=_mongodb_image_patch,
        jira_project="SERVER",
        run_reproducer_fn=_mongodb_run_reproducer,
        reproducer_workload_fn=_mongodb_reproducer_workload,
    ),
    "cockroachdb": DBBuildSpec(
        name="cockroachdb",
        repo_url="https://github.com/cockroachdb/cockroach",
        github_repo="cockroachdb/cockroach",
        # Release tags: v23.2.4, v24.1.0, v24.3.1, …
        version_tag_pattern="v{version}",
        # build_image / build_cmd / artifact_glob / artifact_dest are unused
        # when prebuilt_from_stock=True — kept as placeholders for the schema.
        build_image="golang:1.22",
        build_cmd="true",
        artifact_glob="UNUSED",
        base_image="cockroachdb/cockroach:v{version}",
        artifact_dest="/cockroach/cockroach",
        # The CockroachDB source is a large Go monorepo that requires bazelisk,
        # node, protobuf, and ~30 minutes per clean build.  Publicly-reported
        # bugs that surface via SQL exist in the stock published images
        # (cockroachdb/cockroach:v{version}), so the "custom" image simply
        # re-tags the stock one.  The source tree is still cloned for static
        # analysis by diagnosis oracles.
        prebuilt_from_stock=True,
        # cockroach-operator was sunset; the supported Kubernetes path is now
        # the direct cockroachdb/cockroachdb chart, which renders a StatefulSet
        # with no CRD.  helm_deploy_chart=True tells GenericDBApplication to
        # treat the Helm release itself as the cluster (no CR to apply/patch).
        operator_helm_repo="cockroachdb",
        operator_helm_repo_url="https://charts.cockroachdb.com/",
        operator_chart="cockroachdb/cockroachdb",
        # Under helm_deploy_chart, operator_namespace doubles as the cluster
        # namespace — the release goes here and the StatefulSet pods live here.
        operator_namespace="cockroachdb",
        default_cluster_name="sregym-cockroachdb",
        # cr_kind / cluster_manifest_fn are unused when helm_deploy_chart=True.
        cr_kind="",
        cluster_manifest_fn=None,
        image_patch_fn=_cockroachdb_image_patch,
        helm_deploy_chart=True,
        # Single-node, insecure, openebs-hostpath — matches what kind exposes
        # and keeps the cluster footprint small.  image.tag is injected by
        # _install_operator based on self.version (can't be static here).
        # fullnameOverride ties Service names to cluster_name so the reproducer
        # can resolve {cluster_name}-public deterministically regardless of
        # release-name quirks.
        # tls.certs.selfSigner.enabled=false drops a cert-rotation CronJob that
        # the chart still emits with apiVersion batch/v1beta1 (removed in
        # Kubernetes 1.25) even when tls.enabled=false.
        operator_extra_helm_args=(
            "--set statefulset.replicas=1 "
            "--set tls.enabled=false "
            "--set tls.certs.selfSigner.enabled=false "
            "--set tls.certs.selfSigner.rotateCerts=false "
            "--set storage.persistentVolume.size=1Gi "
            "--set storage.persistentVolume.storageClass=openebs-hostpath "
            "--set statefulset.resources.requests.cpu=250m "
            "--set statefulset.resources.requests.memory=512Mi "
            "--set statefulset.resources.limits.cpu=1 "
            "--set statefulset.resources.limits.memory=1Gi"
        ),
        run_reproducer_fn=_cockroachdb_run_reproducer,
        reproducer_workload_fn=_cockroachdb_reproducer_workload,
    ),
    # cockroachdb/errors is a Go library embedded in CockroachDB.  Bugs there
    # manifest via SQL on a running CockroachDB cluster, so deployment is
    # identical to the cockroachdb spec.  The source tree cloned is the errors
    # library itself (for LLM diagnosis), while the stock base image is still
    # cockroachdb/cockroach — _nearest_released_version normalises any library
    # semver that doesn't match a real Docker Hub tag.
    "cockroachdb_errors": DBBuildSpec(
        name="cockroachdb_errors",
        repo_url="https://github.com/cockroachdb/errors",
        github_repo="cockroachdb/errors",
        version_tag_pattern="v{version}",
        build_image="golang:1.22",
        build_cmd="true",
        artifact_glob="UNUSED",
        base_image="cockroachdb/cockroach:v{version}",
        artifact_dest="/cockroach/cockroach",
        prebuilt_from_stock=True,
        operator_helm_repo="cockroachdb",
        operator_helm_repo_url="https://charts.cockroachdb.com/",
        operator_chart="cockroachdb/cockroachdb",
        operator_namespace="cockroachdb",
        default_cluster_name="sregym-cockroachdb",
        cr_kind="",
        cluster_manifest_fn=None,
        image_patch_fn=_cockroachdb_image_patch,
        helm_deploy_chart=True,
        operator_extra_helm_args=(
            "--set statefulset.replicas=1 "
            "--set tls.enabled=false "
            "--set tls.certs.selfSigner.enabled=false "
            "--set tls.certs.selfSigner.rotateCerts=false "
            "--set storage.persistentVolume.size=1Gi "
            "--set storage.persistentVolume.storageClass=openebs-hostpath "
            "--set statefulset.resources.requests.cpu=250m "
            "--set statefulset.resources.requests.memory=512Mi "
            "--set statefulset.resources.limits.cpu=1 "
            "--set statefulset.resources.limits.memory=1Gi"
        ),
        run_reproducer_fn=_cockroachdb_run_reproducer,
        reproducer_workload_fn=_cockroachdb_reproducer_workload,
    ),
    "etcd": DBBuildSpec(
        name="etcd",
        repo_url="https://github.com/etcd-io/etcd",
        github_repo="etcd-io/etcd",
        version_tag_pattern="v{version}",
        build_image="golang:1.24",
        build_cmd="make build",
        artifact_glob="bin/etcd",
        base_image="quay.io/coreos/etcd:v{version}",
        artifact_dest="/usr/local/bin/etcd",
        operator_helm_repo="bitnami",
        operator_helm_repo_url="https://charts.bitnami.com/bitnami",
        operator_chart="bitnami/etcd",
        operator_namespace="etcd",
        default_cluster_name="sregym-etcd",
        cr_kind="",
        cluster_manifest_fn=None,
        image_patch_fn=_etcd_image_patch,
        helm_deploy_chart=True,
        operator_extra_helm_args=(
            "--set global.security.allowInsecureImages=true "
            "--set replicaCount=1 "
            "--set auth.rbac.create=false "
            "--set auth.rbac.allowNoneAuthentication=true "
            "--set persistence.size=1Gi "
            "--set persistence.storageClass=standard "
            "--set resources.requests.cpu=250m "
            "--set resources.requests.memory=256Mi "
            "--set resources.limits.cpu=1 "
            "--set resources.limits.memory=512Mi "
            "--set readinessProbe.enabled=false "
            "--set livenessProbe.enabled=false "
            "--set startupProbe.enabled=false "
            "--set 'customReadinessProbe.exec.command={etcdctl,endpoint,health}' "
            "--set customReadinessProbe.initialDelaySeconds=10 "
            "--set customReadinessProbe.periodSeconds=10 "
            "--set customLivenessProbe.httpGet.path=/livez "
            "--set customLivenessProbe.httpGet.port=2379 "
            "--set customLivenessProbe.initialDelaySeconds=30 "
            "--set customLivenessProbe.periodSeconds=30"
        ),
        run_reproducer_fn=_etcd_run_reproducer,
        reproducer_workload_fn=_etcd_reproducer_workload,
    ),
}
