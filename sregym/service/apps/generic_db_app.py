"""Generic Kubernetes database application driven by a DBBuildSpec.

Lifecycle:
  deploy()             — install operator prereqs + Helm operator + stock cluster CR
  inject_buggy_image() — patch the running CR to use a custom-built image;
                         triggers a rolling restart so pods run the buggy binary
  cleanup()            — delete cluster CR, namespaces, PVs, operator

This class handles any database whose operator follows the standard pattern:
  - Installed via Helm
  - Managed via a single custom resource (CR)
  - Image swappable via a JSON merge-patch on the CR
"""

import json
import logging
import os
import subprocess
import time

from sregym.service.db_build_spec import DBBuildSpec
from sregym.service.helm import Helm

logger = logging.getLogger("all.application")


def _run(cmd: str, input: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        cmd, shell=True, check=False,
        capture_output=True, text=True, input=input,
    )
    if result.stdout:
        logger.debug(result.stdout.strip())
    if result.stderr:
        logger.debug(result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout


class GenericDBApplication:
    """Deploy and manage a database cluster for any DB type registered in DB_REGISTRY."""

    def __init__(
        self,
        spec: DBBuildSpec,
        version: str,
        cluster_name: str | None = None,
        initial_image: str | None = None,
        extra_helm_args: str = "",
    ):
        self.spec = spec
        self.version = version
        self.cluster_name = cluster_name or spec.default_cluster_name
        self.namespace = spec.operator_namespace
        self.name = spec.name
        self.initial_image = initial_image
        self.extra_helm_args = extra_helm_args

    # ── Public API ────────────────────────────────────────────────────────────

    def deploy(self):
        """Install operator + deploy cluster, wait for it to be ready.

        When spec.helm_deploy_chart is True, the Helm release IS the cluster
        (no separate CR) — _deploy_cluster is a no-op and the image choice is
        passed to _install_operator via --set values.
        """
        logger.info(f"Deploying {self.spec.name} {self.version} via {self.spec.operator_chart}")
        if self.spec.prereqs_fn:
            self.spec.prereqs_fn()
        self._install_operator(custom_image=self.initial_image)
        if not self.spec.helm_deploy_chart:
            self._deploy_cluster(custom_image=self.initial_image)
        self._wait_for_cluster_ready()
        logger.info(f"{self.spec.name} cluster deployed")

    def _helm_release_name(self) -> str:
        """Helm release name.

        For helm_deploy_chart DBs the release name doubles as the StatefulSet/Service
        prefix, so pin it to cluster_name — that's what reproducer helpers build
        service DNS from (e.g. ``{cluster_name}-public``).
        """
        if self.spec.helm_deploy_chart:
            return self.cluster_name
        return f"{self.spec.name}-operator"

    def inject_buggy_image(self, image_tag: str):
        """Patch the running cluster to use a custom-built (buggy) image.

        First gives the operator a chance to propagate the CR change itself.
        If the operator stalls (e.g. sets partition=1 or ignores spec.*.image),
        falls back to scaling it down and patching StatefulSets directly.

        For helm_deploy_chart DBs there is no CR — go straight to the direct
        StatefulSet patch path.
        """
        logger.info(f"Swapping {self.spec.name} cluster to image: {image_tag}")
        if self.spec.helm_deploy_chart:
            self._operator_override(image_tag)
        else:
            patch = self.spec.image_patch_fn(self.cluster_name, self.namespace, image_tag)
            patch_json = json.dumps(patch)
            _run(
                f"kubectl patch {self.spec.cr_kind} {self.cluster_name} "
                f"-n {self.namespace} --type=merge -p '{patch_json}'"
            )
        logger.info("Image patched — waiting for rolling restart")
        self._wait_for_image_rollout(image_tag, timeout=1200)
        logger.info(f"Cluster ready with image: {image_tag}")

    def restore_stock_image(self, custom_image: str | None = None):
        """Patch the cluster back to the stock image and wait for all pods to be Ready.

        Scales the operator back up first in case it was scaled down during fault injection.
        Passes custom_image as the 'from' image for the StatefulSet override fallback so
        containers running the buggy image are correctly identified and replaced.
        """
        stock_image = self.spec.resolved_base_image(self.version)
        logger.info(f"[GenericDBApp] Restoring {self.spec.name} cluster to stock image: {stock_image}")
        self._scale_operator_up()
        time.sleep(5)

        if self.spec.helm_deploy_chart:
            self._operator_override(stock_image, from_image=custom_image)
        else:
            patch = self.spec.image_patch_fn(self.cluster_name, self.namespace, stock_image)
            _run(
                f"kubectl patch {self.spec.cr_kind} {self.cluster_name} "
                f"-n {self.namespace} --type=merge -p '{json.dumps(patch)}'"
            )
        self._wait_for_image_rollout(stock_image, from_image=custom_image)
        # Re-scale operator up in case the fallback override scaled it down during rollout
        self._scale_operator_up()
        logger.info(f"[GenericDBApp] Cluster restored to stock image: {stock_image}")

    def inject_buggy_image_expect_crash(self, image_tag: str, timeout: int = 300):
        """Swap to the buggy image and wait for pods to enter CrashLoopBackOff.

        Used for startup-crash bugs where the new binary fails during bootstrap.
        Applies the same operator-override fallback as inject_buggy_image() so
        the image actually reaches the pods even if the operator stalls.
        """
        logger.info(f"Swapping {self.spec.name} cluster to image: {image_tag} (expecting startup crash)")
        if self.spec.helm_deploy_chart:
            self._operator_override(image_tag)
        else:
            patch = self.spec.image_patch_fn(self.cluster_name, self.namespace, image_tag)
            patch_json = json.dumps(patch)
            _run(
                f"kubectl patch {self.spec.cr_kind} {self.cluster_name} "
                f"-n {self.namespace} --type=merge -p '{patch_json}'"
            )
        self._wait_for_crash_loop(image_tag, timeout)

    def cleanup(self):
        """Delete cluster CR, namespaces, leftover PVs, and the operator."""
        logger.info(f"Cleaning up {self.spec.name} deployment")

        # No CR exists for helm_deploy_chart DBs — skip the kubectl delete.
        if not self.spec.helm_deploy_chart:
            _run(
                f"kubectl delete {self.spec.cr_kind} {self.cluster_name} "
                f"-n {self.namespace} --ignore-not-found",
                check=False,
            )

        self._delete_namespace(self.namespace)

        # Remove any PVs still bound to this namespace.
        pvs = _run(
            f"kubectl get pv --no-headers | grep '{self.namespace}' || true",
            check=False,
        )
        for line in pvs.strip().splitlines():
            if line:
                pv_name = line.split()[0]
                subprocess.run(
                    f'kubectl patch pv {pv_name} -p \'{{"metadata":{{"finalizers":null}}}}\'',
                    shell=True, check=False,
                )
                subprocess.run(
                    f"kubectl delete pv {pv_name} --ignore-not-found",
                    shell=True, check=False,
                )

        release = self._helm_release_name()
        subprocess.run(
            f"helm uninstall {release} -n {self.spec.operator_namespace} 2>/dev/null || true",
            shell=True, check=False,
        )
        self._delete_namespace(self.spec.operator_namespace)
        logger.info(f"{self.spec.name} cleanup complete")

    def run_reproducer(self, reproducer: str):
        """Run a reproducer script/query against the live cluster to trigger the bug."""
        if self.spec.run_reproducer_fn:
            self.spec.run_reproducer_fn(self.cluster_name, self.namespace, reproducer)
        else:
            logger.warning(f"No run_reproducer_fn defined for {self.spec.name} — skipping")

    def run_reproducer_background(self, reproducer: str) -> subprocess.Popen:
        """Fire-and-forget: start the reproducer in a background process and return immediately.

        Useful when the reproducer must be running concurrently with a fault
        (e.g. kill a node mid-schema-change to trigger a rollback path).
        The caller is responsible for waiting on or terminating the returned Popen object.
        """
        if not self.spec.run_reproducer_fn:
            raise RuntimeError(f"No run_reproducer_fn defined for {self.spec.name}")
        import tempfile, os
        # Write the reproducer to a temp file so the subprocess can read it
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False)
        tmp.write(reproducer)
        tmp.flush()
        tmp.close()
        cmd = self.spec.run_reproducer_fn.__module__  # not used — build cmd manually below

        # Resolve the shell command the same way run_reproducer_fn does, but
        # launch it in a Popen instead of waiting for it.
        # We re-use run_reproducer_fn by running it in a thread so any DB-specific
        # logic (e.g. mongosh vs cockroach sql) is preserved.
        import threading
        result_holder = {}
        def _run_in_thread():
            try:
                self.spec.run_reproducer_fn(self.cluster_name, self.namespace, reproducer)
                result_holder["done"] = True
            except Exception as e:
                result_holder["error"] = e

        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()
        os.unlink(tmp.name)
        logger.info(f"[{self.spec.name}] reproducer started in background thread")
        return t

    def kill_pod(self, pod_name: str, wait: bool = True):
        """Delete a specific pod by name and optionally wait for it to be replaced.

        The cluster operator will restart the pod automatically. If wait=True,
        blocks until a pod with the same app label is Running and Ready again.
        """
        ns = self.namespace
        # Grab the app label from the pod before deleting it
        label_out = subprocess.run(
            f"kubectl get pod {pod_name} -n {ns} "
            f"--no-headers -o jsonpath='{{.metadata.labels.app\\.kubernetes\\.io/instance}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip() or self.cluster_name

        logger.info(f"[{self.spec.name}] Killing pod {pod_name} in {ns}")
        _run(f"kubectl delete pod {pod_name} -n {ns} --grace-period=0 --force", check=False)

        if wait:
            logger.info(f"[{self.spec.name}] Waiting for cluster to recover after pod kill")
            self._wait_for_cluster_ready()

    def kill_random_db_pod(self, wait: bool = True):
        """Delete a random database pod and optionally wait for the cluster to recover.

        Useful for triggering failure/rollback paths in schema change jobs, leader
        elections, and other operations that only surface bugs under node loss.
        """
        out = _run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            check=False,
        )
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        if not pods:
            raise RuntimeError(
                f"No pods found for cluster {self.cluster_name!r} in {self.namespace!r}"
            )
        import random
        victim = random.choice(pods)
        logger.info(f"[{self.spec.name}] Randomly selected pod to kill: {victim}")
        self.kill_pod(victim, wait=wait)

    def deploy_continuous_reproducer(self, reproducer: str, expected_output: str | None = None):
        """Deploy a Deployment on the cluster that runs the reproducer in a loop."""
        if not self.spec.reproducer_workload_fn:
            logger.warning(f"No reproducer_workload_fn for {self.spec.name} — skipping continuous workload")
            return
        manifest = self.spec.reproducer_workload_fn(self.cluster_name, self.namespace, reproducer, expected_output)
        _run("kubectl apply -f -", input=manifest)
        logger.info(
            f"Continuous reproducer workload '{self.cluster_name}-reproducer' "
            f"deployed in '{self.namespace}'"
            + (f" (checking for expected_output={expected_output!r})" if expected_output else "")
        )

    def start_workload(self):
        pass

    def create_workload(self, **kwargs):
        pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _install_operator(self, custom_image: str | None = None):
        logger.info(f"Installing {self.spec.operator_chart} in '{self.spec.operator_namespace}'")
        try:
            Helm.add_repo(self.spec.operator_helm_repo, self.spec.operator_helm_repo_url)
            Helm.repo_update()
        except RuntimeError as e:
            logger.warning(f"Helm repo setup issue (continuing with cached charts): {e}")

        release = self._helm_release_name()
        existing = subprocess.run(
            f"helm status {release} -n {self.spec.operator_namespace}",
            shell=True, capture_output=True, text=True,
        )
        if existing.returncode == 0:
            if "deployed" in existing.stdout:
                logger.info(f"{self.spec.name} operator already deployed, skipping")
                return
            if "failed" in existing.stdout:
                subprocess.run(
                    f"helm uninstall {release} -n {self.spec.operator_namespace}",
                    shell=True, check=False,
                )

        # Chart-only DBs (helm_deploy_chart) drop the operator-flavoured flags
        # (global.clusterScoped) and instead inject the DB image via --set.
        # fullnameOverride pins the StatefulSet/Service names to cluster_name
        # so reproducer helpers resolve `{cluster_name}-public` deterministically.
        if self.spec.helm_deploy_chart:
            stock = self.spec.resolved_base_image(self.version)
            image_repo, _, image_tag = stock.rpartition(":")
            if custom_image:
                repo, _, tag = custom_image.rpartition(":")
                if repo and tag:
                    image_repo, image_tag = repo, tag
            # Bitnami charts split the image into registry + repository.
            # Detect non-Docker-Hub registries (contain a dot before the
            # first slash) and set image.registry separately.
            parts = image_repo.split("/", 1)
            if len(parts) == 2 and "." in parts[0]:
                image_flags = (
                    f"--set image.registry={parts[0]} "
                    f"--set image.repository={parts[1]} "
                    f"--set image.tag={image_tag} "
                )
            else:
                image_flags = (
                    f"--set image.repository={image_repo} --set image.tag={image_tag} "
                )
            # No --wait for helm_deploy_chart: the cockroachdb chart ships a
            # separate ``cockroach init`` Job that bootstraps the cluster, and
            # the StatefulSet can't pass its readiness probe until that Job
            # runs.  helm --wait only blocks on the StatefulSet, so it times
            # out opaquely while init is still pending.  _wait_for_cluster_ready
            # polls with visible logging and tolerates the longer bring-up.
            helm_cmd = (
                f"helm upgrade --install {release} {self.spec.operator_chart} "
                f"--namespace {self.spec.operator_namespace} "
                f"--create-namespace "
                f"--set fullnameOverride={self.cluster_name} "
                f"{image_flags}"
                f"{self.spec.operator_extra_helm_args} "
                f"{self.extra_helm_args}"
            )
        else:
            helm_cmd = (
                f"helm upgrade --install {release} {self.spec.operator_chart} "
                f"--namespace {self.spec.operator_namespace} "
                f"--create-namespace "
                f"--set global.clusterScoped=true "
                f"{self.spec.operator_extra_helm_args} "
                f"{self.extra_helm_args} "
                f"--wait --timeout 5m"
            )
        for attempt in range(1, 4):
            try:
                _run(helm_cmd)
                break
            except RuntimeError as e:
                if attempt == 3 or "webhook" not in str(e):
                    raise
                logger.warning(f"Operator install attempt {attempt}/3 failed — retrying in 20s")
                time.sleep(20)

        logger.info(f"{self.spec.name} operator ready")

    def _deploy_cluster(self, custom_image: str | None):
        _run(
            f"kubectl create namespace {self.namespace} "
            f"--dry-run=client -o yaml | kubectl apply -f -"
        )
        manifest = self.spec.cluster_manifest_fn(
            self.cluster_name, self.namespace, self.version, custom_image
        )
        _run("kubectl apply -f -", input=manifest)
        logger.info(f"{self.spec.name} cluster CR applied to '{self.namespace}'")

    def _wait_for_cluster_ready(self, timeout: int | None = None):
        """Wait until the cluster's own pods are Ready.

        Uses app.kubernetes.io/instance={cluster_name}, which all major operators
        apply to every pod they manage (TiDB, K8ssandra, etc.).

        Requires the pod count to be stable across two consecutive 15-second
        polls before declaring success.  This prevents returning early when the
        operator is still creating pods (e.g. PD ready before TiKV/TiDB exist).

        The timeout defaults to 1200s (raise via the SREGYM_CLUSTER_READY_TIMEOUT
        env var). A multi-node first boot — e.g. a 3-node K8ssandra cluster on a
        cold kind node — can take ~11 minutes to reach all-pods-Ready, which
        exceeded the previous 600s cap and made deploy() spuriously fail even
        though the cluster came up healthy moments later.
        """
        if timeout is None:
            try:
                timeout = int(os.environ.get("SREGYM_CLUSTER_READY_TIMEOUT", "1200"))
            except ValueError:
                timeout = 1200
        label = f"app.kubernetes.io/instance={self.spec.resolved_ready_instance_label(self.cluster_name)}"
        # Exclude one-shot Job pods that share the instance label but terminate
        # Completed/Failed — e.g. cockroachdb's cluster-init Job.  They can
        # never pass condition=Ready, which would otherwise stall this loop.
        field_sel = "status.phase!=Succeeded,status.phase!=Failed"
        logger.info(
            f"Waiting for cluster '{self.cluster_name}' pods "
            f"(label: {label}) to be Ready (up to {timeout}s)…"
        )
        deadline = time.time() + timeout
        last_count = 0
        stable_and_ready = 0

        while time.time() < deadline:
            out = subprocess.run(
                f"kubectl get pods -n {self.namespace} -l '{label}' "
                f"--field-selector='{field_sel}' --no-headers 2>/dev/null",
                shell=True, capture_output=True, text=True,
            )
            pods = [l for l in out.stdout.strip().splitlines() if l]
            count = len(pods)

            if count > 0 and count == last_count:
                r = subprocess.run(
                    f"kubectl wait pods -n {self.namespace} -l '{label}' "
                    f"--field-selector='{field_sel}' "
                    f"--for=condition=Ready --timeout=5s",
                    shell=True, capture_output=True, text=True,
                )
                if r.returncode == 0:
                    stable_and_ready += 1
                    if stable_and_ready >= 2:
                        logger.info(f"Cluster '{self.cluster_name}' is Ready ({count} pods)")
                        return
                else:
                    stable_and_ready = 0
            else:
                stable_and_ready = 0

            last_count = count
            time.sleep(15)

        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for cluster '{self.cluster_name}' "
            f"pods to be Ready in '{self.namespace}'"
        )

    def _scale_operator_up(self):
        """Scale every operator Deployment back to 1 replica."""
        deps_out = subprocess.run(
            f"kubectl get deployments -n {self.spec.operator_namespace} "
            f"--no-headers -o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for dep in deps_out.stdout.strip().split():
            logger.info(f"[GenericDBApp] Scaling up operator deployment '{dep}'")
            subprocess.run(
                f"kubectl scale deployment {dep} -n {self.spec.operator_namespace} --replicas=1",
                shell=True, capture_output=True,
            )

    def _wait_for_image_rollout(self, image_tag: str, timeout: int = 600, from_image: str | None = None):
        """Wait until at least one pod in the namespace runs image_tag and is Ready.

        Gives the operator up to 30 s to propagate the CR change on its own.
        If no pod shows the new image by then, falls back to a generic override:
          1. Scale down every Deployment in the operator namespace so the operator
             stops reconciling (and reverting) the StatefulSets.
          2. For every StatefulSet in the cluster namespace:
             - Reset partition to 0 so Kubernetes' own rolling-update fires.
             - Replace any container whose image matches the stock base image with
               image_tag (identified by resolved_base_image from the spec).
        This fallback is operator-agnostic: it uses no hardcoded names.
        """
        logger.info(
            f"Waiting for rolling update to image '{image_tag}' "
            f"in '{self.namespace}' (up to {timeout}s)…"
        )
        override_attempted = False
        deadline = time.time() + timeout
        operator_wait = time.time() + 30  # grace period before override

        while time.time() < deadline:
            if self._any_pod_has_image(image_tag):
                pod_name = self._find_pod_with_image(image_tag)
                if pod_name:
                    ready_r = subprocess.run(
                        f"kubectl wait pod/{pod_name} -n {self.namespace} "
                        f"--for=condition=Ready --timeout=5s",
                        shell=True, capture_output=True, text=True,
                    )
                    if ready_r.returncode == 0:
                        logger.info(f"Pod '{pod_name}' is Ready with image '{image_tag}'")
                        return

            if not override_attempted and time.time() > operator_wait:
                logger.info(
                    "Operator did not propagate image change — "
                    "falling back to direct StatefulSet override"
                )
                self._operator_override(image_tag, from_image=from_image)
                override_attempted = True

            time.sleep(15)

        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for image '{image_tag}' "
            f"to roll out in '{self.namespace}'"
        )

    def _wait_for_crash_loop(self, image_tag: str, timeout: int = 300):
        """Wait until a pod running image_tag enters CrashLoopBackOff or exits non-zero.

        Applies the same operator-override fallback as _wait_for_image_rollout so
        the new image actually reaches pods even when the operator stalls.
        """
        import json as _json
        logger.info(
            f"Waiting for crash-loop on image '{image_tag}' "
            f"in '{self.namespace}' (up to {timeout}s)…"
        )
        override_attempted = False
        deadline = time.time() + timeout
        operator_wait = time.time() + 30

        while time.time() < deadline:
            if self._any_pod_has_image(image_tag):
                out = subprocess.run(
                    f"kubectl get pods -n {self.namespace} -o json 2>/dev/null",
                    shell=True, capture_output=True, text=True,
                )
                try:
                    data = _json.loads(out.stdout)
                    for pod in data.get("items", []):
                        for cs in pod.get("status", {}).get("containerStatuses", []):
                            if image_tag not in cs.get("image", ""):
                                continue
                            waiting_reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
                            exit_code = cs.get("state", {}).get("terminated", {}).get("exitCode")
                            if waiting_reason in ("CrashLoopBackOff", "Error") or (
                                exit_code is not None and exit_code != 0
                            ):
                                pod_name = pod["metadata"]["name"]
                                logger.info(
                                    f"Startup crash confirmed on pod '{pod_name}': "
                                    f"reason={waiting_reason or 'exit'} exitCode={exit_code}"
                                )
                                return
                except Exception:
                    pass

            if not override_attempted and time.time() > operator_wait:
                logger.info(
                    "Operator did not propagate image change — "
                    "falling back to direct StatefulSet override"
                )
                self._operator_override(image_tag)
                override_attempted = True

            time.sleep(10)

        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for crash-loop on image '{image_tag}' "
            f"in '{self.namespace}'"
        )

    def _any_pod_has_image(self, image_tag: str) -> bool:
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-o jsonpath='{{range .items[*]}}{{range .spec.containers[*]}}{{.image}} {{end}}{{end}}' "
            f"2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        return image_tag in out.stdout

    def _find_pod_with_image(self, image_tag: str) -> str | None:
        names_out = subprocess.run(
            f"kubectl get pods -n {self.namespace} --no-headers "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for pod_name in names_out.stdout.strip().split():
            img_out = subprocess.run(
                f"kubectl get pod {pod_name} -n {self.namespace} "
                f"-o jsonpath='{{range .spec.containers[*]}}{{.image}} {{end}}' 2>/dev/null",
                shell=True, capture_output=True, text=True,
            )
            if image_tag in img_out.stdout:
                return pod_name
        return None

    def _operator_override(self, image_tag: str, from_image: str | None = None):
        """Scale down all operator Deployments, then directly patch StatefulSets.

        from_image: the image currently running in pods (used for container matching).
        Defaults to the stock base image (fault-inject direction); pass the custom image
        for recovery (stock-restore direction).
        """
        deps_out = subprocess.run(
            f"kubectl get deployments -n {self.spec.operator_namespace} "
            f"--no-headers -o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for dep in deps_out.stdout.strip().split():
            logger.info(f"Scaling down operator deployment '{dep}'")
            subprocess.run(
                f"kubectl scale deployment {dep} -n {self.spec.operator_namespace} --replicas=0",
                shell=True, capture_output=True,
            )
        time.sleep(5)

        match_image = from_image if from_image is not None else self.spec.resolved_base_image(self.version)
        sts_out = subprocess.run(
            f"kubectl get statefulsets -n {self.namespace} --no-headers "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for sts in sts_out.stdout.strip().split():
            self._patch_statefulset(sts, match_image, image_tag)

    def _patch_statefulset(self, sts_name: str, base_image: str | None, image_tag: str):
        """Reset partition to 0 and replace matching containers with image_tag.

        base_image: image currently on the containers to replace. None means patch all containers.
        """
        base_repo = base_image.split(":")[0] if base_image else None
        ctrs_out = subprocess.run(
            f"kubectl get statefulset {sts_name} -n {self.namespace} "
            f"-o jsonpath='{{range .spec.template.spec.containers[*]}}{{.name}} {{.image}}|{{end}}' "
            f"2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        containers_to_patch = []
        for entry in ctrs_out.stdout.strip().split("|"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(" ", 1)
            if len(parts) == 2:
                ctr_name, ctr_image = parts
                if base_repo is None or base_repo in ctr_image or ctr_image == base_image:
                    containers_to_patch.append({"name": ctr_name, "image": image_tag})

        if not containers_to_patch:
            logger.warning(
                f"No containers matching '{base_repo or 'any'}' found in StatefulSet '{sts_name}' — "
                "skipping image patch, resetting partition only"
            )

        patch = {"spec": {"updateStrategy": {"rollingUpdate": {"partition": 0}}}}
        if containers_to_patch:
            patch["spec"]["template"] = {"spec": {"containers": containers_to_patch}}  # type: ignore[index]

        logger.info(
            f"Patching StatefulSet '{sts_name}': "
            f"partition=0, containers={[c['name'] for c in containers_to_patch]}"
        )
        subprocess.run(
            f"kubectl patch statefulset {sts_name} -n {self.namespace} "
            f"--type=strategic -p '{json.dumps(patch)}'",
            shell=True, check=True,
        )

    @staticmethod
    def _delete_namespace(namespace: str):
        subprocess.run(
            f"kubectl delete namespace {namespace} --ignore-not-found",
            shell=True, check=False,
        )
