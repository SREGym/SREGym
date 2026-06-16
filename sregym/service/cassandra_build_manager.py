"""Builds custom Cassandra Docker images from user-modified source files.

Workflow:
  1. Overlay modified .java files from a patch directory onto the cloned source tree
  2. Run ``ant jar`` to compile a patched apache-cassandra-<version>.jar
  3. Build a Docker image extending k8ssandra/cass-management-api with the patched JAR
  4. Load the image into the local kind cluster

Build results are cached by a SHA-256 hash of the patch files — if the hash
matches an already-present local Docker image the compile/build steps are skipped.

Patch directory layout mirrors the Cassandra source tree, e.g.:

    patches/my_bug/
        src/java/org/apache/cassandra/db/PartitionColumns.java
        src/java/org/apache/cassandra/db/SerializationHeader.java

Only files present in the patch directory are overwritten; everything else in
the cloned source is left as-is.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# K8ssandra management API base image — Cassandra lives inside this image
_MGMT_API_BASE = "k8ssandra/cass-management-api:{version}-ubi8"

# Path inside the management API image where the main Cassandra JAR lives
_CASSANDRA_LIB_DIR = "/opt/cassandra/lib"

# Bump this to invalidate all cached images when the Dockerfile template changes.
_DOCKERFILE_VERSION = "v3"


def _kind_name_arg() -> str:
    """Return ``--name <cluster>`` for the active kind context, else ``""``.

    ``kind load docker-image`` defaults to the cluster named ``kind``; when the
    benchmark targets a differently-named kind cluster (e.g. a per-worker
    ``kind-val2`` in a multi-cluster fan-out) the image must be loaded with an
    explicit ``--name`` or it lands in the wrong/absent cluster and the pods
    ImagePullBackOff. kind names its kubectl context ``kind-<clustername>``, so
    the cluster name is the current context with the leading ``kind-`` stripped.
    Returns ``""`` for non-kind contexts (preserving prior behaviour and letting
    the SSH/DaemonSet fallbacks handle real clusters).
    """
    env = os.environ.copy()
    if env.get("SREGYM_REAL_KUBECONFIG"):
        env["KUBECONFIG"] = env["SREGYM_REAL_KUBECONFIG"]
    r = subprocess.run("kubectl config current-context", shell=True, capture_output=True, text=True, env=env)
    ctx = r.stdout.strip()
    if r.returncode == 0 and ctx.startswith("kind-"):
        return f" --name {ctx[len('kind-') :]}"
    return ""


class CassandraBuildManager:
    """Compile patched Cassandra source and package it into a Docker image."""

    def __init__(self, source_path: Path, cassandra_version: str):
        self.source_path = Path(source_path)
        self.cassandra_version = cassandra_version
        self.base_image = _MGMT_API_BASE.format(version=cassandra_version)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_from_directory(self) -> str:
        """Build an image from the current state of ``self.source_path``.

        Use this after an agent has edited files under ``/opt/source``.
        The image tag is derived from a hash of all ``.java`` files under
        ``src/java/`` so unchanged source produces the same cached tag.
        """
        java_root = self.source_path / "src" / "java"
        if not java_root.exists():
            raise FileNotFoundError(f"Java source root not found: {java_root}")

        source_hash = self._hash_dir(java_root)
        versioned = hashlib.sha256(f"{_DOCKERFILE_VERSION}:{source_hash}".encode()).hexdigest()
        image_tag = f"sregym/cassandra-patched:{self.cassandra_version}-{versioned[:8]}"

        if self._image_exists_locally(image_tag):
            logger.info(f"[BuildMgr] Cached image {image_tag} found — skipping build")
            self._load_into_kind(image_tag)
            return image_tag

        logger.info(f"[BuildMgr] Building image from current source tree: {image_tag}")
        self._build_jar()
        self._build_docker_image(image_tag)
        self._load_into_kind(image_tag)
        logger.info(f"[BuildMgr] Image ready: {image_tag}")
        return image_tag

    def build_with_patches(self, patch_dir: Path) -> str:
        """Apply patches, build JAR, package Docker image.  Returns image name:tag.

        The returned image is guaranteed to be present in the local Docker daemon
        and loaded into the kind cluster (if kind is running).

        IMPORTANT: Patches are ALWAYS applied to the source tree, even if the
        Docker image is cached. This ensures the agent sees the buggy code at
        /opt/source when investigating.
        """
        patch_dir = Path(patch_dir)
        if not patch_dir.exists():
            raise FileNotFoundError(f"Patch directory not found: {patch_dir}")

        # ALWAYS apply patches to source so agent can see the buggy code.
        # This must happen before the cache check.
        self._apply_patches(patch_dir)

        patch_hash = self._hash_dir(patch_dir)
        versioned = hashlib.sha256(f"{_DOCKERFILE_VERSION}:{patch_hash}".encode()).hexdigest()
        image_tag = f"sregym/cassandra-patched:{self.cassandra_version}-{versioned[:8]}"

        if self._image_exists_locally(image_tag):
            logger.info(f"[BuildMgr] Cached image {image_tag} found — skipping build")
            self._load_into_kind(image_tag)  # re-load in case cluster was recreated
            return image_tag

        logger.info(f"[BuildMgr] Building custom Cassandra image {image_tag}")
        self._build_jar()
        self._build_docker_image(image_tag)
        self._load_into_kind(image_tag)
        logger.info(f"[BuildMgr] Custom image ready: {image_tag}")
        return image_tag

    # ------------------------------------------------------------------
    # Build steps
    # ------------------------------------------------------------------

    def _apply_patches(self, patch_dir: Path):
        """Overwrite source files with modified versions from patch_dir."""
        patched = []
        for patch_file in sorted(patch_dir.rglob("*.java")):
            relative = patch_file.relative_to(patch_dir)
            dest = self.source_path / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(patch_file, dest)
            patched.append(str(relative))

        if patched:
            logger.info(f"[BuildMgr] Patched {len(patched)} file(s): {patched}")
        else:
            logger.warning("[BuildMgr] No .java files found in patch directory")

    @staticmethod
    def _java_major_version() -> int:
        """Return the major version of the active JDK (e.g. 11, 17, 23)."""
        try:
            out = subprocess.run(
                ["java", "-version"], capture_output=True, text=True
            ).stderr  # java -version writes to stderr
            # First token like '23.0.1', '11.0.22', '1.8.0_392'
            import re

            m = re.search(r'"(\d+)(?:\.(\d+))?', out)
            if m:
                major = int(m.group(1))
                return 8 if major == 1 else major  # handle '1.8' → 8
        except Exception:
            pass
        return 0

    def _build_jar(self):
        """Compile the patched source with ``ant clean jar``.

        Cassandra 4.x requires JDK 11.  If the system JDK is too new (>= 12)
        the compilation is run inside an eclipse-temurin:11 Docker container
        that has ant pre-installed, with the source tree bind-mounted.

        Always runs ``ant clean`` first to ensure no stale class files.
        """
        java_ver = self._java_major_version()
        use_docker = java_ver == 0 or java_ver >= 12

        if use_docker:
            logger.info(
                f"[BuildMgr] System JDK {java_ver} is not JDK 11 — compiling inside eclipse-temurin:11 Docker container"
            )
            self._build_jar_in_docker()
        else:
            self._require_ant()
            logger.info(
                f"[BuildMgr] Running `ant clean jar` locally (JDK {java_ver}) in {self.source_path}  "
                "(clean build ~5 min)"
            )
            result = subprocess.run(
                "ant clean jar -Duse.jdk11=true",
                cwd=self.source_path,
                shell=True,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "CASSANDRA_USE_JDK11": "true"},
            )
            if result.returncode != 0:
                out = result.stdout[-3000:] if result.stdout else ""
                err = result.stderr[-3000:] if result.stderr else ""
                raise RuntimeError(f"ant jar failed:\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
            logger.info("[BuildMgr] ant jar succeeded")

    def _build_jar_in_docker(self):
        """Run ``ant clean jar`` inside an eclipse-temurin:11 container (JDK 11 + apt ant)."""
        logger.info(
            f"[BuildMgr] Docker-based ant clean build — source: {self.source_path}  "
            "(first run pulls image + installs ant, ~5-10 min)"
        )
        # The bind-mount uses the same path inside the container for simplicity.
        cmd = (
            f"docker run --rm "
            f"-v {self.source_path}:{self.source_path} "
            f"-w {self.source_path} "
            f"eclipse-temurin:11 "
            f"bash -c 'apt-get update -qq && apt-get install -y -q ant && "
            f"ant clean jar -Duse.jdk11=true'"
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            out = result.stdout[-3000:] if result.stdout else ""
            err = result.stderr[-3000:] if result.stderr else ""
            raise RuntimeError(f"Docker ant jar failed:\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
        logger.info("[BuildMgr] Docker ant jar succeeded")

    def _build_docker_image(self, image_tag: str):
        """Build a Docker image extending the K8ssandra base with the patched JAR."""
        jar_name = f"apache-cassandra-{self.cassandra_version}.jar"
        jar_src = self.source_path / "build" / jar_name

        if not jar_src.exists():
            # Fallback: look for SNAPSHOT variant (built without -Dversion override)
            candidates = sorted((self.source_path / "build").glob(f"apache-cassandra-{self.cassandra_version}*.jar"))
            if candidates:
                jar_src = candidates[0]
                jar_name = jar_src.name
                logger.info(f"[BuildMgr] Using JAR: {jar_src.name}")
            else:
                raise FileNotFoundError(
                    f"Built JAR not found at {jar_src}. Make sure `ant jar` completed successfully."
                )

        build_ctx = Path(tempfile.mkdtemp(prefix="sregym-cassandra-docker-"))
        try:
            # Always install as the canonical name so it replaces the base image's JAR.
            canonical_jar = f"apache-cassandra-{self.cassandra_version}.jar"
            shutil.copy2(jar_src, build_ctx / canonical_jar)
            dockerfile = "\n".join(
                [
                    f"FROM {self.base_image}",
                    "USER root",
                    f"COPY {canonical_jar} {_CASSANDRA_LIB_DIR}/{canonical_jar}",
                    # No-argument helper script for -XX:OnOutOfMemoryError (avoids spaces in JVM flag)
                    r"RUN printf '#!/bin/sh\nkill -9 1\n' > /usr/local/bin/oom-kill-mgmt.sh && chmod +x /usr/local/bin/oom-kill-mgmt.sh",
                    # Comment out the default OOM handler in cassandra-env.sh so our custom handler
                    # (set via additionalJvm11ServerOptions) is not overridden
                    r"RUN sed -i 's/^JVM_ON_OUT_OF_MEMORY_ERROR_OPT=/#JVM_ON_OUT_OF_MEMORY_ERROR_OPT=/' /opt/cassandra/conf/cassandra-env.sh",
                ]
            )
            (build_ctx / "Dockerfile").write_text(dockerfile)

            platform = self._cluster_platform()
            logger.info(f"[BuildMgr] Building Docker image {image_tag} for {platform}")
            result = subprocess.run(
                f"docker buildx build --platform {platform} --load -t {image_tag} .",
                cwd=build_ctx,
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"docker buildx build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
            logger.info(f"[BuildMgr] Docker image {image_tag} built")
        finally:
            shutil.rmtree(build_ctx, ignore_errors=True)

    def _load_into_kind(self, image_tag: str):
        """Distribute the image to every node in the active cluster.

        Strategy (tried in order):
          1. kind        — ``kind load docker-image`` (kind clusters only)
          2. SSH         — ``docker save | gzip | ssh <node> sudo docker load``
                           Configurable via env vars:
                             SREGYM_CLUSTER_SSH_USER  (default: current OS user)
                             SREGYM_CLUSTER_SSH_KEY   (default: SSH agent / ~/.ssh/id_rsa)
          3. DaemonSet   — privileged DS that mounts the Docker socket and loads
                           via ``kubectl cp`` + ``docker load``; works on any
                           Docker-runtime cluster without SSH access
        """
        # 1. kind
        name_arg = _kind_name_arg()
        if (
            subprocess.run(
                f"kind load docker-image {image_tag}{name_arg}",
                shell=True,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        ):
            logger.info(f"[BuildMgr] Loaded {image_tag} into kind cluster")
            return

        # 2. SSH
        node_ips = self._get_cluster_node_ips()
        if node_ips:
            ssh_user = self._ssh_user()
            ssh_opts = self._ssh_opts()
            logger.info(f"[BuildMgr] Distributing {image_tag} to {len(node_ips)} node(s) as {ssh_user} via SSH")
            failed = []
            for ip in node_ips:
                cmd = (
                    f"docker save {image_tag} | gzip | ssh {ssh_opts} {ssh_user}@{ip} 'sudo docker load || docker load'"
                )
                logger.info(f"[BuildMgr] → pushing to {ip} …")
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if r.returncode == 0:
                    logger.info(f"[BuildMgr] Image loaded on {ip}")
                else:
                    logger.warning(f"[BuildMgr] SSH load failed on {ip}: {r.stderr.strip()}")
                    failed.append(ip)

            if not failed:
                return  # all nodes got the image
            logger.info(f"[BuildMgr] SSH failed on {failed} — falling back to DaemonSet approach")

        # 3. DaemonSet (Docker socket)
        self._load_via_daemonset(image_tag)

    @staticmethod
    def _get_cluster_node_ips() -> list[str]:
        """Return all node InternalIP addresses from the current kubectl context."""
        result = subprocess.run(
            "kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type==\"InternalIP\")].address}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [ip for ip in result.stdout.strip().strip("'").split() if ip]

    @staticmethod
    def _ssh_user() -> str:
        """SSH username for cluster nodes.

        Set SREGYM_CLUSTER_SSH_USER to override; defaults to the current OS user.
        """
        import getpass
        import os

        return os.environ.get("SREGYM_CLUSTER_SSH_USER", "") or getpass.getuser()

    @staticmethod
    def _ssh_opts() -> str:
        """Common SSH options.  Set SREGYM_CLUSTER_SSH_KEY for a custom identity file."""
        import os

        opts = "-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"
        if key := os.environ.get("SREGYM_CLUSTER_SSH_KEY", ""):
            opts += f" -i {key}"
        return opts

    def _load_via_daemonset(self, image_tag: str):
        """Load the image on every node via a privileged DaemonSet + kubectl cp.

        Works on any cluster whose nodes use the Docker container runtime and
        where the Docker socket is exposed at /var/run/docker.sock.
        """
        import uuid

        ns = "default"
        ds_name = f"sregym-img-loader-{uuid.uuid4().hex[:6]}"
        logger.info(f"[BuildMgr] Deploying image-loader DaemonSet {ds_name}")

        manifest = f"""\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {ds_name}
  namespace: {ns}
spec:
  selector:
    matchLabels:
      app: {ds_name}
  template:
    metadata:
      labels:
        app: {ds_name}
    spec:
      tolerations:
      - operator: Exists
      containers:
      - name: loader
        image: docker:cli
        command: ["sleep", "infinity"]
        volumeMounts:
        - name: docker-sock
          mountPath: /var/run/docker.sock
      volumes:
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
"""
        try:
            # Deploy DS
            subprocess.run(
                "kubectl apply -f -",
                shell=True,
                input=manifest,
                capture_output=True,
                text=True,
                check=True,
            )

            # Wait until all DS pods are Ready
            logger.info("[BuildMgr] Waiting for DaemonSet pods to be Ready …")
            subprocess.run(
                f"kubectl rollout status daemonset/{ds_name} -n {ns} --timeout=120s",
                shell=True,
                capture_output=True,
                text=True,
            )
            pods_result = subprocess.run(
                f"kubectl get pods -n {ns} -l app={ds_name} "
                f"-o jsonpath='{{.items[?(@.status.phase==\"Running\")].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            pods = [p for p in pods_result.stdout.strip().strip("'").split() if p]
            logger.info(f"[BuildMgr] DaemonSet pods: {pods}")

            for pod in pods:
                logger.info(f"[BuildMgr] Piping image into {pod} …")
                # Stream docker save directly into docker load via kubectl exec stdin
                r = subprocess.run(
                    f"docker save {image_tag} | kubectl exec -n {ns} -i {pod} -- docker load",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                if r.returncode == 0:
                    logger.info(f"[BuildMgr] Image loaded on node hosting {pod}")
                else:
                    logger.warning(f"[BuildMgr] Failed to load on {pod}: {r.stderr.strip()}")

        finally:
            subprocess.run(
                f"kubectl delete daemonset {ds_name} -n {ns} --ignore-not-found",
                shell=True,
                capture_output=True,
            )
            logger.info("[BuildMgr] Image-loader DaemonSet cleaned up")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cluster_platform() -> str:
        """Return the Docker platform string matching the cluster nodes.

        Queries the first node's architecture via kubectl and maps it to a
        Docker platform string (e.g. ``linux/amd64``, ``linux/arm64``).
        Falls back to the host's native platform if kubectl isn't available.
        """
        import platform as _platform

        try:
            result = subprocess.run(
                "kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}'",
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            arch = result.stdout.strip().strip("'")  # 'amd64' or 'arm64'
            if arch:
                return f"linux/{arch}"
        except Exception:
            pass
        # Fallback: native host arch
        machine = _platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            return "linux/arm64"
        return "linux/amd64"

    def _image_exists_locally(self, image_tag: str) -> bool:
        result = subprocess.run(
            f"docker image inspect {image_tag}",
            shell=True,
            capture_output=True,
        )
        return result.returncode == 0

    @staticmethod
    def _require_ant():
        if shutil.which("ant") is not None:
            return

        logger.info("[CassandraBuildManager] ant not found — attempting automatic installation")

        # Try brew (macOS / Linuxbrew)
        brew = shutil.which("brew") or "/home/linuxbrew/.linuxbrew/bin/brew"
        if shutil.which("brew") or Path("/home/linuxbrew/.linuxbrew/bin/brew").exists():
            result = subprocess.run(
                f"{brew} install ant",
                shell=True,
            )
            if result.returncode == 0 and shutil.which("ant"):
                logger.info("[CassandraBuildManager] ant installed via brew")
                return

        # Try apt-get (Debian/Ubuntu)
        if shutil.which("apt-get"):
            result = subprocess.run(
                "sudo apt-get install -y ant || apt-get install -y ant",
                shell=True,
            )
            if result.returncode == 0 and shutil.which("ant"):
                logger.info("[CassandraBuildManager] ant installed via apt-get")
                return

        # Fallback: download tarball
        ant_script = Path(__file__).parents[2] / "tests" / "e2e-testing-scripts" / "ant.sh"
        if ant_script.exists():
            result = subprocess.run(["bash", str(ant_script)], check=False)
            if shutil.which("ant"):
                logger.info("[CassandraBuildManager] ant installed via ant.sh")
                return

        if shutil.which("ant") is None:
            raise RuntimeError(
                "Apache Ant is required to build Cassandra from source but could not be "
                "installed automatically. Install it manually:\n"
                "  macOS:   brew install ant\n"
                "  Ubuntu:  sudo apt-get install -y ant\n"
            )

    @staticmethod
    def _hash_dir(directory: Path) -> str:
        """SHA-256 over sorted file paths + contents in a directory."""
        h = hashlib.sha256()
        for f in sorted(directory.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(directory)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()
