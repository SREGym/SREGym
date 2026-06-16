"""Generic database build manager driven by a DBBuildSpec.

Mirrors CassandraBuildManager but works for any database type.

Two entry points:
  build_from_directory()   — source tree already contains the bug (cloned at
                             the buggy commit via GitHubIssueParser).  Used by
                             auto-generated problems.
  build_with_patches()     — apply hand-crafted patch files first, then build.
                             Used by hand-crafted problems.

Both return an image tag that is:
  - present in the local Docker daemon
  - loaded into the active cluster (kind / SSH / DaemonSet fallback)
"""

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from sregym.service.db_build_spec import DBBuildSpec

logger = logging.getLogger(__name__)

_DOCKERFILE_VERSION = "v1"


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


def _kind_worker_nodes_arg() -> str:
    """Return ``--nodes <worker nodes>`` to load images only into worker nodes.

    The control-plane node runs no DB ring pods (it is NoSchedule-tainted), yet
    ``kind load`` copies every image into ALL nodes including the control-plane,
    where it is never used. Those unused images accumulate orphaned containerd
    snapshots that can only be reclaimed by restarting the control-plane's
    containerd — which restarts the static etcd pod and crash-loops the
    openebs/disk provisioner (a chronic disk-pressure source in long fan-out
    runs). Restricting loads to worker nodes avoids the leak entirely. Fails
    safe to ``""`` (load into all nodes, prior behaviour) whenever the worker
    set can't be determined.
    """
    name_arg = _kind_name_arg().strip()
    if not name_arg:
        return ""
    cluster = name_arg.split()[-1]
    r = subprocess.run(
        f"kind get nodes --name {cluster}",
        shell=True, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ""
    workers = [n for n in r.stdout.split() if n and "control-plane" not in n]
    if not workers:
        return ""
    return " --nodes " + ",".join(workers)


class GenericDBBuildManager:
    def __init__(self, spec: DBBuildSpec, source_path: Path, version: str):
        self.spec = spec
        self.source_path = Path(source_path)
        self.version = version

    # ── Public API ────────────────────────────────────────────────────────────

    def build_from_directory(self) -> str:
        """Build an image from the current source tree (already in buggy state).

        Image tag is derived from a hash of the full source tree so that
        identical source produces the same cached image.
        """
        source_hash = self._hash_dir(self.source_path)
        image_tag = self._make_tag(source_hash)

        if self._image_exists_locally(image_tag):
            logger.info(f"[GenericBuildMgr] Cached image {image_tag} — skipping build")
            self._load_into_cluster(image_tag)
            return image_tag

        if self.spec.prebuilt_from_stock:
            logger.info(
                f"[GenericBuildMgr] prebuilt_from_stock — tagging stock base as {image_tag}"
            )
            self._build_prebuilt_image(image_tag)
            self._load_into_cluster(image_tag)
            return image_tag

        logger.info(f"[GenericBuildMgr] Building from source tree: {image_tag}")
        self._build_artifact()
        self._build_docker_image(image_tag)
        self._load_into_cluster(image_tag)
        return image_tag

    def build_with_patches(self, patch_dir: Path) -> str:
        """Apply patch files, build, package.  Returns image name:tag."""
        patch_dir = Path(patch_dir)
        if not patch_dir.exists():
            raise FileNotFoundError(f"Patch directory not found: {patch_dir}")

        patch_hash = self._hash_dir(patch_dir)
        image_tag = self._make_tag(patch_hash)

        if self._image_exists_locally(image_tag):
            logger.info(f"[GenericBuildMgr] Cached image {image_tag} — skipping build")
            self._load_into_cluster(image_tag)
            return image_tag

        logger.info(f"[GenericBuildMgr] Building with patches: {image_tag}")
        self._apply_patches(patch_dir)
        self._build_artifact()
        self._build_docker_image(image_tag)
        self._load_into_cluster(image_tag)
        return image_tag

    # ── Build steps ───────────────────────────────────────────────────────────

    def _apply_patches(self, patch_dir: Path):
        patched = []
        for patch_file in sorted(patch_dir.rglob("*")):
            if patch_file.is_file():
                relative = patch_file.relative_to(patch_dir)
                dest = self.source_path / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(patch_file, dest)
                patched.append(str(relative))
        if patched:
            logger.info(f"[GenericBuildMgr] Patched {len(patched)} file(s)")
        else:
            logger.warning("[GenericBuildMgr] No files found in patch directory")

    def _resolve_build_image(self) -> str:
        """For Go-based builds, detect the required Go version from go.mod."""
        import re
        if not self.spec.build_image.startswith("golang:"):
            return self.spec.build_image
        go_mod = self.source_path / "go.mod"
        if not go_mod.exists():
            return self.spec.build_image
        m = re.search(r"^go\s+(\d+\.\d+)", go_mod.read_text(), re.MULTILINE)
        if not m:
            return self.spec.build_image
        go_ver = m.group(1)
        detected = f"golang:{go_ver}"
        # Pin to Debian Bullseye (glibc 2.31) so the binary is compatible with
        # Alpine+glibc-compat base images (e.g. TiDB) that don't have glibc 2.32+.
        # The untagged golang:X.Y is now Bookworm (glibc 2.36) which produces
        # binaries that crash with "GLIBC_2.32 not found" in those containers.
        # Fall back to -bookworm if -bullseye doesn't exist for this Go version.
        if not any(s in detected for s in ("-bullseye", "-bookworm", "-alpine", "-buster")):
            import subprocess as _sp
            for suffix in ("-bullseye", "-bookworm"):
                candidate = f"golang:{go_ver}{suffix}"
                ok = _sp.run(
                    f"docker manifest inspect {candidate}",
                    shell=True, capture_output=True,
                )
                if ok.returncode == 0:
                    detected = candidate
                    break
            else:
                detected += "-bookworm"
        if detected != self.spec.build_image:
            logger.info(
                f"[GenericBuildMgr] go.mod requires Go {go_ver} — "
                f"overriding build image {self.spec.build_image!r} → {detected!r}"
            )
        return detected

    def _build_artifact(self):
        """Run spec.build_cmd inside the resolved build image with the source tree mounted."""
        import os
        import tempfile
        build_image = self._resolve_build_image()
        host_uid = os.getuid()
        host_gid = os.getgid()
        logger.info(
            f"[GenericBuildMgr] Building {self.spec.name} inside {build_image} "
            f"— command: {self.spec.build_cmd}"
        )
        # Docker runs as root inside the container and writes root-owned files
        # into the bind-mounted source tree. Without intervention, the host user
        # (who invoked this tool) can never delete those files — breaking
        # `git clean -fdx` in SourceManager.reset_source() and corrupting every
        # subsequent build. We fix this by:
        #   (1) chown-ing any pre-existing cruft back to the host user on entry,
        #       so leftovers from older builds are fixed up automatically, and
        #   (2) installing an EXIT trap that chowns everything created during
        #       this build back to the host user — even if the build fails.
        script_content = (
            "#!/bin/bash\nset -e\n"
            f"chown -R {host_uid}:{host_gid} {self.source_path} 2>/dev/null || true\n"
            f"trap 'chown -R {host_uid}:{host_gid} {self.source_path} 2>/dev/null || true' EXIT\n"
            "command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -q --no-install-recommends git)\n"
            f"git config --global --add safe.directory {self.source_path}\n"
            f"{self.spec.build_cmd}\n"
        )
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.sh', prefix='/tmp/sregym-build-', delete=False
        ) as f:
            f.write(script_content)
            script_path = f.name
        os.chmod(script_path, 0o755)
        try:
            cmd = (
                f"docker run --rm "
                f"--network=host "
                f"-v {self.source_path}:{self.source_path} "
                f"-v {script_path}:{script_path} "
                f"-w {self.source_path} "
                f"{build_image} "
                f"bash {script_path}"
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                out = result.stdout[-3000:] if result.stdout else ""
                err = result.stderr[-3000:] if result.stderr else ""
                raise RuntimeError(
                    f"Build failed for {self.spec.name}:\n"
                    f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
                )
            logger.info("[GenericBuildMgr] Build succeeded")
        finally:
            os.unlink(script_path)

    def _build_prebuilt_image(self, image_tag: str):
        """Create a thin `FROM <base>` image and tag it as `image_tag`.

        Used when `prebuilt_from_stock=True` — the custom image IS the stock
        base image, just re-tagged so it's distinct from the deploy-time tag
        (lets the pipeline's image-swap step still cause a rolling restart).
        """
        base_image = self._resolve_base_image()
        subprocess.run(
            f"docker pull {base_image}",
            shell=True, capture_output=True, text=True,
        )
        build_ctx = Path(tempfile.mkdtemp(prefix=f"sregym-{self.spec.name}-docker-"))
        try:
            (build_ctx / "Dockerfile").write_text(f"FROM {base_image}\n")
            platform = self._cluster_platform()
            logger.info(f"[GenericBuildMgr] docker buildx build {image_tag} for {platform} (prebuilt)")
            result = subprocess.run(
                f"docker buildx build --platform {platform} --provenance=false --sbom=false --load -t {image_tag} .",
                cwd=build_ctx,
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"docker buildx build (prebuilt) failed:\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}"
                )
            logger.info(f"[GenericBuildMgr] Prebuilt image tagged: {image_tag}")
        finally:
            shutil.rmtree(build_ctx, ignore_errors=True)

    def _build_docker_image(self, image_tag: str):
        _exclude = ("-tests.jar", "-sources.jar", "-javadoc.jar", "-test-sources.jar")
        artifacts = [
            a for a in sorted(self.source_path.glob(self.spec.artifact_glob))
            if not any(a.name.endswith(s) for s in _exclude)
        ]
        if not artifacts:
            bin_contents = sorted(
                str(p.relative_to(self.source_path))
                for p in self.source_path.rglob("*")
                if p.is_file() and p.stat().st_size > 1_000_000
            )[:20]
            raise FileNotFoundError(
                f"No artifact matched '{self.spec.artifact_glob}' under {self.source_path}.\n"
                f"Large files found (possible binaries): {bin_contents}"
            )
        artifact = artifacts[0]
        if len(artifacts) > 1:
            logger.warning(f"[GenericBuildMgr] Multiple artifacts matched, using {artifact.name}")

        base_image = self._resolve_base_image()
        artifact_dest = self.spec.resolved_artifact_dest(self.version)
        build_ctx = Path(tempfile.mkdtemp(prefix=f"sregym-{self.spec.name}-docker-"))
        try:
            dest_filename = artifact.name
            shutil.copy2(artifact, build_ctx / dest_filename)
            dockerfile = "\n".join([
                f"FROM {base_image}",
                "USER root",
                f"COPY {dest_filename} {artifact_dest}",
            ])
            (build_ctx / "Dockerfile").write_text(dockerfile)

            platform = self._cluster_platform()
            logger.info(f"[GenericBuildMgr] docker buildx build {image_tag} for {platform}")
            result = subprocess.run(
                f"docker buildx build --platform {platform} --provenance=false --sbom=false --load -t {image_tag} .",
                cwd=build_ctx,
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"docker buildx build failed:\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}"
                )
            logger.info(f"[GenericBuildMgr] Image built: {image_tag}")
        finally:
            shutil.rmtree(build_ctx, ignore_errors=True)

    def _resolve_base_image(self) -> str:
        """Return the base image for the Docker build.

        Uses the version-specific image from the spec if it exists on Docker Hub.
        Falls back to the runtime base from the source tree's own Dockerfile so
        that pre-release versions (e.g. v9.0.0-dev) still build correctly.
        """
        candidate = self.spec.resolved_base_image(self.version)
        check = subprocess.run(
            f"docker manifest inspect {candidate}",
            shell=True, capture_output=True,
        )
        if check.returncode == 0:
            return candidate

        # Parse the runtime FROM from the source Dockerfile (last non-builder FROM)
        dockerfile = self.source_path / "Dockerfile"
        if dockerfile.exists():
            froms = [
                line.split()[1]
                for line in dockerfile.read_text().splitlines()
                if line.strip().upper().startswith("FROM") and " as builder" not in line.lower()
            ]
            if froms:
                logger.info(
                    f"[GenericBuildMgr] Base image {candidate!r} not found — "
                    f"falling back to source Dockerfile base: {froms[-1]!r}"
                )
                return froms[-1]

        raise RuntimeError(
            f"Base image {candidate!r} not found on Docker Hub and no Dockerfile fallback available."
        )

    # ── Cluster image distribution ────────────────────────────────────────────
    # Same three-strategy fallback as CassandraBuildManager:
    #   1. kind load docker-image
    #   2. SSH push (docker save | gzip | ssh node docker load)
    #   3. Privileged DaemonSet via kubectl cp + docker load

    def _load_into_cluster(self, image_tag: str):
        name_arg = _kind_name_arg()
        nodes_arg = _kind_worker_nodes_arg()
        kind_r = subprocess.run(
            f"kind load docker-image {image_tag}{name_arg}{nodes_arg}",
            shell=True, capture_output=True, text=True,
        )
        if kind_r.returncode == 0:
            logger.info(f"[GenericBuildMgr] Loaded {image_tag} into kind cluster")
            return
        # Log the reason — silent kind failures used to send us down SSH/DaemonSet
        # fallbacks that are both broken on modern kind (no sshd, containerd not docker).
        logger.warning(
            f"[GenericBuildMgr] kind load failed for {image_tag}: "
            f"{(kind_r.stderr or kind_r.stdout).strip()[:500]}"
        )

        node_ips = self._get_cluster_node_ips()
        if node_ips:
            ssh_user = self._ssh_user()
            ssh_opts = self._ssh_opts()
            failed = []
            for ip in node_ips:
                cmd = (
                    f"docker save {image_tag} | gzip | "
                    f"ssh {ssh_opts} {ssh_user}@{ip} 'sudo docker load || docker load'"
                )
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if r.returncode == 0:
                    logger.info(f"[GenericBuildMgr] Image loaded on {ip}")
                else:
                    logger.warning(f"[GenericBuildMgr] SSH load failed on {ip}: {r.stderr.strip()}")
                    failed.append(ip)
            if not failed:
                return

        self._load_via_daemonset(image_tag)

    @staticmethod
    def _get_cluster_node_ips() -> list[str]:
        result = subprocess.run(
            "kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type==\"InternalIP\")].address}'",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        return [ip for ip in result.stdout.strip().strip("'").split() if ip]

    @staticmethod
    def _ssh_user() -> str:
        import getpass
        import os
        return os.environ.get("SREGYM_CLUSTER_SSH_USER", "") or getpass.getuser()

    @staticmethod
    def _ssh_opts() -> str:
        import os
        opts = "-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"
        if key := os.environ.get("SREGYM_CLUSTER_SSH_KEY", ""):
            opts += f" -i {key}"
        return opts

    def _load_via_daemonset(self, image_tag: str):
        import uuid
        ns = "default"
        ds_name = f"sregym-img-loader-{uuid.uuid4().hex[:6]}"
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
            subprocess.run(
                "kubectl apply -f -", shell=True, input=manifest,
                capture_output=True, text=True, check=True,
            )
            subprocess.run(
                f"kubectl rollout status daemonset/{ds_name} -n {ns} --timeout=120s",
                shell=True, capture_output=True, text=True,
            )
            pods_result = subprocess.run(
                f"kubectl get pods -n {ns} -l app={ds_name} "
                f"-o jsonpath='{{.items[?(@.status.phase==\"Running\")].metadata.name}}'",
                shell=True, capture_output=True, text=True,
            )
            pods = [p for p in pods_result.stdout.strip().strip("'").split() if p]
            for pod in pods:
                r = subprocess.run(
                    f"docker save {image_tag} | kubectl exec -n {ns} -i {pod} -- docker load",
                    shell=True, capture_output=True, text=True,
                )
                if r.returncode == 0:
                    logger.info(f"[GenericBuildMgr] Image loaded via DaemonSet pod {pod}")
                else:
                    logger.warning(f"[GenericBuildMgr] DaemonSet load failed on {pod}: {r.stderr.strip()}")
        finally:
            subprocess.run(
                f"kubectl delete daemonset {ds_name} -n {ns} --ignore-not-found",
                shell=True, capture_output=True,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_tag(self, content_hash: str) -> str:
        versioned = hashlib.sha256(
            f"{_DOCKERFILE_VERSION}:{content_hash}".encode()
        ).hexdigest()
        return f"sregym/{self.spec.name}-patched:{self.version}-{versioned[:8]}"

    def _image_exists_locally(self, image_tag: str) -> bool:
        return subprocess.run(
            f"docker image inspect {image_tag}",
            shell=True, capture_output=True,
        ).returncode == 0

    @staticmethod
    def _cluster_platform() -> str:
        import platform as _platform
        try:
            result = subprocess.run(
                "kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}'",
                shell=True, capture_output=True, text=True, timeout=10,
            )
            arch = result.stdout.strip().strip("'")
            if arch:
                return f"linux/{arch}"
        except Exception:
            pass
        machine = _platform.machine().lower()
        return "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"

    @staticmethod
    def _hash_dir(directory: Path) -> str:
        h = hashlib.sha256()
        for f in sorted(directory.rglob("*")):
            if f.is_symlink():
                continue  # skip symlinks — build-created symlinks aren't source
            try:
                if f.is_file():
                    h.update(str(f.relative_to(directory)).encode())
                    h.update(f.read_bytes())
            except (PermissionError, OSError):
                pass
        return h.hexdigest()
