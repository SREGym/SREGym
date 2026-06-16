"""Base class for problems that build and deploy a custom (buggy) database binary.

Two usage modes:

  Auto-generated (issue-driven)
  ──────────────────────────────
  Point at a GitHub issue.  The parser resolves the DB type, version, and buggy
  git ref automatically.  The source tree at that commit IS the buggy state —
  no patch files needed.

      class AutoCassandra20108(GenericCustomBuildProblem):
          db_name   = "cassandra"
          issue_url = "https://github.com/apache/cassandra/issues/20108"

  Hand-crafted (patch-driven)
  ────────────────────────────
  Supply a patch directory and explicit version.  Modified source files are
  overlaid on a clean clone before building.

      class MyCassandraBug(GenericCustomBuildProblem):
          db_name        = "cassandra"
          db_version     = "4.1.7"
          source_git_ref = "cassandra-4.1.7"
          patch_dir      = Path(__file__).parent / "patches" / "my_bug"
          root_cause_description = "..."

Problem lifecycle
─────────────────
  __init__   — parse issue (if issue-driven) → clone source → build image
  deploy()   — install operator + deploy STOCK cluster (called by conductor)
  inject_fault() — swap running cluster to the buggy image (rolling restart)
  recover_fault() — no-op; conductor tears down the cluster between problems
"""

import dataclasses
import logging
import re
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.reproducer_pod_mitigation import ReproducerPodMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.generic_db_app import GenericDBApplication
from sregym.service.db_build_spec import DB_REGISTRY, DBBuildSpec
from sregym.service.generic_db_build_manager import GenericDBBuildManager
from sregym.service.issue_parser import parse_issue
from sregym.service.source_manager import SourceManager
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


def _nearest_released_version(spec, version: str) -> str:
    """Return version if its Docker image exists, otherwise the nearest released
    version on the same repo (preferring same MAJOR.MINOR, else latest stable).

    Handles the common case where the issue parser extracts a partial version
    (e.g. ``25.2`` when Docker Hub only publishes ``v25.2.0`` … ``v25.2.17``) or
    a pre-release that hasn't been tagged yet.
    """
    import subprocess as _sp
    candidate = spec.resolved_base_image(version)
    ok = _sp.run(f"docker manifest inspect {candidate}", shell=True, capture_output=True)
    if ok.returncode == 0:
        return version

    # Everything before the `:` in the base_image template is the Docker Hub
    # repo. Works for cockroachdb/cockroach, pingcap/tidb,
    # k8ssandra/cass-management-api, mongodb/mongodb-community-server, etc.
    repo = spec.base_image.split(":", 1)[0]
    logger.warning(
        f"[GenericCustomBuild] {candidate!r} not on Docker Hub — "
        f"querying {repo!r} tags for nearest match"
    )
    try:
        import urllib.request as _ur, json as _js
        url = (
            f"https://registry.hub.docker.com/v2/repositories/{repo}"
            f"/tags?page_size=100&ordering=last_updated"
        )
        with _ur.urlopen(url, timeout=10) as r:
            tags = [t["name"] for t in _js.loads(r.read())["results"]]
    except Exception as e:
        logger.warning(f"[GenericCustomBuild] Docker Hub tag query failed: {e}")
        return version

    # Tags can be v1.2.3, 1.2.3-ubi8, r1.2.3-fips, etc. — extract the bare
    # semver. No `\b` anchor at the start: `v` is a word-char, so `\bv26…`
    # would have no boundary before `26` and the match would fail.
    def _bare_semver(tag: str) -> str | None:
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag)
        return ".".join(m.groups()) if m else None

    semvers = sorted(
        {v for v in (_bare_semver(t) for t in tags) if v},
        key=lambda v: tuple(int(x) for x in v.split(".")),
        reverse=True,
    )
    if not semvers:
        return version

    parts = version.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        prefix = f"{parts[0]}.{parts[1]}."
        same_minor = [v for v in semvers if v.startswith(prefix)]
        if same_minor:
            resolved = same_minor[0]
            logger.info(
                f"[GenericCustomBuild] Resolved {version!r} → {resolved!r} "
                f"(newest same-minor release on {repo})"
            )
            return resolved

    resolved = semvers[0]
    logger.info(
        f"[GenericCustomBuild] Resolved {version!r} → {resolved!r} "
        f"(latest stable on {repo}; no same-minor tag available)"
    )
    return resolved


def _version_from_source(source_path: Path, spec) -> str | None:
    """Extract version from known version files in the source tree."""
    candidates = [
        # TiDB: pkg/parser/mysql/const.go — TiDBReleaseVersion = "v8.4.0-..."
        source_path / "pkg" / "parser" / "mysql" / "const.go",
        # Cassandra: version files
        source_path / "build.xml",
        source_path / "pom.xml",
        # MongoDB: version file in buildscripts
        source_path / "buildscripts" / "mongo_version.py",
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        m = re.search(r'[Rr]elease[Vv]ersion\s*=\s*"v?(\d+\.\d+\.\d+)', text)
        if m:
            return m.group(1)
        m = re.search(r'<version>(\d+\.\d+\.\d+)', text)
        if m:
            return m.group(1)
        # MongoDB: mongo_version.py — version = "7.0.5"
        m = re.search(r'^version\s*=\s*["\'](\d+\.\d+\.\d+)', text, re.MULTILINE)
        if m:
            return m.group(1)
    return None


class GenericCustomBuildProblem(Problem):
    """Deploy any database at a specific buggy version and swap the image at fault-inject time.

    Subclasses must set ``db_name`` and either ``issue_url`` (auto mode) or
    ``db_version`` + ``source_git_ref`` + ``patch_dir`` (hand-crafted mode).
    """

    # ── Required ─────────────────────────────────────────────────────────────
    db_name: str   # key into DB_REGISTRY, e.g. "cassandra"

    # ── Auto mode (issue-driven) ──────────────────────────────────────────────
    issue_url: str | None = None

    # ── Hand-crafted mode (patch-driven) ─────────────────────────────────────
    db_version: str | None = None        # e.g. "4.1.7"
    source_git_ref: str | None = None    # e.g. "cassandra-4.1.7"
    patch_dir: Path | None = None

    # ── Optional overrides ────────────────────────────────────────────────────
    # If not set in auto mode, populated from the issue title + body.
    root_cause_description: str = ""
    root_cause_file: str = "source"
    # Script/query to run after the buggy image is active to trigger the bug.
    # If not set, populated from the parsed issue body.
    reproducer: str | None = None
    # For wrong-result bugs: the BUGGY value the query returns/persists (NOT the correct
    # value). The reproducer-pod readiness probe greps for this value, so Ready = bug still
    # present, Not Ready = fixed (see ReproducerPodMitigationOracle and the skill's oracle notes).
    expected_output: str | None = None
    continuous_reproducer: bool = False
    # Set True for bugs that crash the DB process on startup (not at query time).
    # inject_fault() will call setup_preconditions() first (to set up required
    # cluster state while the stock binary is still running), then swap to the
    # buggy binary and wait for a crash-loop rather than a Ready pod.
    crash_on_startup: bool = False
    # SQL / cluster-setting commands to run before the main reproducer (e.g. to
    # force a schema change job to fail and roll back). Populated from the issue
    # body in auto mode; can be overridden in hand-crafted mode.
    _setup_preconditions_sql: str | None = None
    # Extra --set flags appended to the Helm install for this specific problem.
    # Allows per-problem customization (e.g. persistence, env vars, sidecars)
    # without modifying the shared DBBuildSpec.
    extra_helm_args: str = ""
    # Per-problem overrides for the build step.  When set, these replace the
    # corresponding fields on the shared DBBuildSpec *for this problem only*.
    build_cmd: str | None = None
    build_image: str | None = None
    # When True, skip compilation: the "custom" buggy image is the stock base
    # image re-tagged (used when the bug already ships in the released image,
    # i.e. buggy version = fix patch - 1). Overrides DBBuildSpec.prebuilt_from_stock
    # for this problem only. Leave None to inherit the spec default.
    prebuilt_from_stock: bool | None = None

    # ── Init ─────────────────────────────────────────────────────────────────

    def __init__(self):
        if self.db_name not in DB_REGISTRY:
            raise ValueError(
                f"Unknown db_name '{self.db_name}'. "
                f"Add an entry to DB_REGISTRY in db_build_spec.py."
            )
        spec: DBBuildSpec = DB_REGISTRY[self.db_name]

        overrides = {}
        if self.build_cmd is not None:
            overrides["build_cmd"] = self.build_cmd
        if self.build_image is not None:
            overrides["build_image"] = self.build_image
        if self.prebuilt_from_stock is not None:
            overrides["prebuilt_from_stock"] = self.prebuilt_from_stock
        if overrides:
            spec = dataclasses.replace(spec, **overrides)
            logger.info(f"[GenericCustomBuild] Per-problem spec overrides: {overrides}")

        version, git_ref = self._resolve_version_and_ref(spec)
        source_path = SourceManager().ensure_source(
            repo_url=spec.repo_url,
            git_ref=git_ref,
            name=spec.name,
        )

        _major = int(version.split(".")[0]) if re.match(r'^\d', version) else 0
        if version == "unknown" or _major == 0:
            version = _version_from_source(source_path, spec) or version
            logger.info(f"[GenericCustomBuild] Resolved version from source tree: {version}")

        # For pre-release versions whose Docker image doesn't exist, use the nearest
        # released version for the stock cluster while the custom binary still comes
        # from the actual source commit.
        deploy_version = _nearest_released_version(spec, version)
        if deploy_version != version:
            logger.info(
                f"[GenericCustomBuild] {version!r} has no Docker image — "
                f"deploying stock cluster at {deploy_version!r}"
            )

        # Build manager uses its version to pull the stock base image — must be
        # a real Docker Hub tag, not the partial/pre-release version from the
        # issue. Source tree already checked out at git_ref, so the buggy code
        # is preserved regardless of which stock tag we stack it on top of.
        build_mgr = GenericDBBuildManager(spec, source_path, deploy_version)
        if self.patch_dir is not None:
            self._custom_image = build_mgr.build_with_patches(Path(self.patch_dir))
        else:
            self._custom_image = build_mgr.build_from_directory()

        logger.info(f"[GenericCustomBuild] Using image: {self._custom_image}")

        # crash_on_startup bugs need the stock cluster up to run setup_preconditions()
        # before the swap, so always deploy with the stock image for those.
        if self.crash_on_startup:
            self._predeployed_buggy = False
            initial_image = None
        else:
            self._predeployed_buggy = deploy_version != version
            initial_image = self._custom_image if self._predeployed_buggy else None
        app = GenericDBApplication(
            spec, deploy_version,
            initial_image=initial_image,
            extra_helm_args=self.extra_helm_args,
        )

        _original_deploy = app.deploy
        def _wrapped_deploy():
            _original_deploy()
            self.post_deploy()
        app.deploy = _wrapped_deploy

        super().__init__(app=app, namespace=app.namespace)

        self.source_code_path = source_path

        root_cause = self.build_structured_root_cause(
            component=f"source/{self.root_cause_file}",
            namespace=self.namespace,
            description=self.root_cause_description or f"Bug in {spec.name} {version}",
        )
        self.root_cause = root_cause
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=root_cause)

        if self.continuous_reproducer:
            self.mitigation_oracle = ReproducerPodMitigationOracle(
                problem=self,
                cluster_name=app.cluster_name,
                expect_unready=self.expected_output is not None,
            )
        else:
            self.mitigation_oracle = None

    def requires_openebs(self) -> bool:
        """These problems deploy operator-managed clusters whose PVCs request the
        ``openebs-hostpath`` StorageClass (see the manifests in db_build_spec.py).
        Returning True makes the Conductor provision OpenEBS so that storage class
        exists, regardless of the ``deploy_openebs`` config flag — mirroring
        CassandraBugProblem.requires_openebs(). Without this, a run with
        ``deploy_openebs=False`` leaves PVCs Pending and the cluster never schedules.
        """
        return True

    def _cqlsh_auth_flags(self) -> str:
        """Return ``-u <user> -p <pw> `` for the K8ssandra superuser so that a bare
        ``cqlsh`` run inside a server pod authenticates (the deployed cluster enables
        PasswordAuthenticator). Returns ``""`` when no ``<cluster>-superuser`` secret
        exists (auth disabled / non-K8ssandra deploy). Cached after the first lookup.

        Generated custom ``inject_fault()`` code drives ``cqlsh`` directly via
        ``kubectl exec``; without these flags every such call fails with
        ``AuthenticationFailed('Remote end requires authentication')`` and the
        reproduction silently never runs.
        """
        cached = getattr(self, "_cqlsh_auth_cache", None)
        if cached is not None:
            return cached
        import base64
        import shlex
        import subprocess

        cluster = getattr(getattr(self, "app", None), "cluster_name", None) or "sregym-cassandra"
        secret = f"{cluster}-superuser"

        def _secret_val(key: str) -> str:
            result = subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.{key}}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            raw = result.stdout.strip()
            if not raw:
                return ""
            try:
                return base64.b64decode(raw).decode()
            except Exception:
                return ""

        user = _secret_val("username")
        pw = _secret_val("password")
        flags = f"-u {shlex.quote(user)} -p {shlex.quote(pw)} " if user and pw else ""
        self._cqlsh_auth_cache = flags
        return flags

    def _authed_cqlsh(self, inner: str) -> str:
        """Insert superuser auth flags into every ``cqlsh`` invocation in a shell
        snippet, leaving ``nodetool`` and other commands untouched. Handles all
        forms (``cqlsh -e``, ``cqlsh -f``, ``| cqlsh``, ``cqlsh 127.0.0.1``). No-op
        when the cluster has no superuser secret."""
        flags = self._cqlsh_auth_flags().strip()
        if not flags:
            return inner
        import re

        return re.sub(r"\bcqlsh\b", f"cqlsh {flags}", inner)

    # ── Fault injection ───────────────────────────────────────────────────────

    def post_deploy(self):
        """Override to run actions after the cluster is deployed and ready.

        Called at the end of app.deploy(). Typical uses: patch StatefulSet
        for tmpfs volumes, add sidecars, apply settings that aren't
        available via Helm values.
        """

    def setup_preconditions(self):
        """Override to prepare cluster state before the buggy image is swapped in.

        Called during inject_fault() while the stock binary is still running.
        Typical uses: enable a feature flag, add a DDL index, insert seed data.
        """
        if self._setup_preconditions_sql:
            logger.info("[GenericCustomBuild] Running extracted setup_preconditions SQL")
            try:
                self.app.run_reproducer(self._setup_preconditions_sql)
            except Exception as e:
                logger.warning(f"[GenericCustomBuild] setup_preconditions SQL raised: {e}")

    @mark_fault_injected
    def inject_fault(self):
        """Swap the running cluster to the buggy image, then trigger the bug."""
        if self.crash_on_startup:
            logger.info("[GenericCustomBuild] Setting up preconditions before fault injection")
            self.setup_preconditions()
            logger.info(f"[GenericCustomBuild] Swapping to buggy image (expect startup crash): {self._custom_image}")
            self.app.inject_buggy_image_expect_crash(self._custom_image)
            logger.info("[GenericCustomBuild] Startup crash confirmed")
            return

        if self._predeployed_buggy:
            logger.info("[GenericCustomBuild] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(
                f"[GenericCustomBuild] Injecting fault: swapping {self.db_name} "
                f"cluster to {self._custom_image}"
            )
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[GenericCustomBuild] Buggy image active")
        self.setup_preconditions()
        if self.reproducer:
            logger.info("[GenericCustomBuild] Running reproducer to trigger bug")
            try:
                self.app.run_reproducer(self.reproducer)
            except Exception as e:
                logger.warning(f"[GenericCustomBuild] run_reproducer raised (expected for crash bugs): {e}")
            if self.continuous_reproducer:
                self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Swap the cluster back to the stock image and wait for it to be Ready."""
        logger.info("[GenericCustomBuild] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[GenericCustomBuild] Recovery complete")

    # ── Internal ─────────────────────────────────────────────────────────────

    def _resolve_version_and_ref(self, spec: DBBuildSpec) -> tuple[str, str]:
        """Return (version, git_ref), resolving from issue URL or class attributes."""
        if self.issue_url:
            parsed = parse_issue(self.issue_url)
            if not self.root_cause_description and parsed.body:
                self.root_cause_description = (
                    f"{parsed.title}\n\n{parsed.body[:1000]}"
                ).strip()
            if not self.reproducer and parsed.reproducer:
                self.reproducer = parsed.reproducer
            if not self.expected_output and parsed.expected_output:
                self.expected_output = parsed.expected_output
            if parsed.crash_on_startup:
                self.crash_on_startup = True
            if not self._setup_preconditions_sql and parsed.setup_preconditions:
                self._setup_preconditions_sql = parsed.setup_preconditions
            return parsed.version, parsed.git_ref

        if self.db_version and self.source_git_ref:
            return self.db_version, self.source_git_ref

        raise ValueError(
            f"{self.__class__.__name__} must set either 'issue_url' (auto mode) "
            f"or both 'db_version' and 'source_git_ref' (hand-crafted mode)."
        )
