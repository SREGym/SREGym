"""Dependency-Track: a release imported across an api-server rollout never raises its new-vulnerable-dependency alerts.

Upstream defect (Dependency-Track 5.3.0-SNAPSHOT at fd55decc, unreported; found
by model checking and reproduced here on the published image):
``ImportBomActivity`` commits the whole import (project, components, graph) in
one transaction inside ``processBom`` and marks every component it inserted with
``Component.isNew``, a ``transient`` field that exists only in that execution's
memory. Only after the commit does it derive the vulnerability-analysis context
file (``vuln-analysis/context/<token>.proto``, the ids of the new components)
from that flag and create the ``analyze-project`` run carrying it.
``ReconcileVulnAnalysisResultsActivity`` emits NEW_VULNERABLE_DEPENDENCY only
from that context. The dex engine runs activities at least once: if the import
is interrupted after the commit (here a rolling update's SIGTERM; the engine
abandons the task at DEBUG and the next pod runs it again), the re-execution
finds every component already present, marks none as new, stores no context and
creates the analysis without one. The analysis then finds the vulnerabilities,
evaluates the policies and sends POLICY_VIOLATION alerts normally, but
NEW_VULNERABLE_DEPENDENCY is never emitted, and no later upload or analysis
emits it because the components are no longer new. Nothing above INFO is logged
on the pod that remains; the interrupted pod's log is gone with it.

The natural event here: the platform team's api-server configuration change
(memory limit 2Gi -> 3Gi through the Helm release) rolls the api-server while
checkout-web's 5.13.0 release is being imported. The release brings
micromatch 6.27.19 (internal advisory INT-2026-0107, HIGH) and axios
(INT-2026-0071, MEDIUM); the violation-tracker gets its ticket for the HIGH
finding's policy violation, but the AppSec team's dependency-review queue never
receives the release's NEW_VULNERABLE_DEPENDENCY notifications, and its audit
warns about the release's vulnerable dependencies without review once they are
15 minutes old. An earlier routine api-server rollout and an earlier release
(reviewed normally) precede it; a frontend rollout and a later release
(reviewed normally) follow it.

Driver (deterministic, invisible at handover): when the release's import-bom
activity has started, the conductor opens a PostgreSQL session that takes
``LOCK TABLE dex_workflow_run IN SHARE ROW EXCLUSIVE MODE``; the import commits
its components and then waits in ``createRun`` for the analysis. The conductor
then applies the Helm change; the RollingUpdate (maxSurge 0) terminates the old
pod, whose engine shutdown interrupts the waiting JDBC call and abandons the
task (the JVM exits within seconds), and the conductor rolls its session back as
soon as the old pod is gone (about 3 s of waiting; the new pod is not yet
running). The new pod re-executes the import about 15 s later. If the lock lands too late (the
analysis run already exists), the conductor lets that release finish without a
rollout and uses the next release. Only the ordinary rollout and the new pod's
normal import log remain.

Reference mitigation (needs the mechanism): make the release's vulnerable
components new again in a fresh import: export the release's BOM
(``GET /api/v1/bom/cyclonedx/project/{uuid}``), delete the vulnerable components
(``DELETE /api/v1/component/{uuid}``) and upload the BOM again
(``PUT /api/v1/bom``), so the import marks them new and the analysis emits the
notifications. Accepted alternative: delete the components and re-run the
release pipeline (push the tag again). Collateral: new component UUIDs,
superseded tickets. Re-analysis, restarts, re-running the pipeline without removing the
components, or waiting do nothing; deleting the project, suppressing the
findings, removing advisories or breaking the rules are rejected; review items
posted by hand (signed with the secret, or written into the notification outbox)
do not count. A blind fix does not work,
and the judge also gates on the mechanism.
"""

import contextlib
import json
import subprocess
import time

from sregym.conductor.oracles.apps.dependency_track.dependency_reviews import DependencyTrackDependencyReviewOracle
from sregym.conductor.oracles.apps.dependency_track.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.dependency_track.application import DependencyTrack
from sregym.utils.decorators import mark_fault_injected

IMPORT_RUNNING = "SELECT count(*) FROM dex_activity_task WHERE activity_name = 'import-bom' AND locked_by IS NOT NULL"
BLOCKED_ON_RUNS = (
    "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'dependency-track%' "
    "AND wait_event_type = 'Lock' AND query ILIKE '%dex_workflow_run%'"
)


class PsqlSession:
    """A persistent ``psql`` in the PostgreSQL pod (stdin), so statements land within milliseconds."""

    def __init__(self, namespace):
        self.process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                namespace,
                "exec",
                "-i",
                "deploy/postgres",
                "--",
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U dtrack -d dtrack -X -q -tA',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.counter = 0

    def run(self, sql, timeout=60):
        self.counter += 1
        marker = f"__done_{self.counter}__"
        self.process.stdin.write(f"{sql.rstrip().rstrip(';')};\n\\echo {marker}\n")
        self.process.stdin.flush()
        lines, deadline = [], time.monotonic() + timeout
        for line in self.process.stdout:
            if line.strip() == marker:
                return "\n".join(lines)
            lines.append(line.rstrip("\n"))
            if time.monotonic() > deadline:
                break
        raise RuntimeError(f"psql did not answer: {sql[:80]} ({lines[-3:]})")

    def close(self, rollback=False):
        with contextlib.suppress(OSError, ValueError):
            self.process.stdin.write(("ROLLBACK;\n" if rollback else "") + "\\q\n")
            self.process.stdin.flush()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def root_cause_text(release="checkout-web 5.13.0", rollout="the api-server rollout", count="two"):
    return (
        f"The dependency-review queue never received the NEW_VULNERABLE_DEPENDENCY notifications for the {count} "
        f"vulnerable dependencies that {release} introduced (micromatch 6.27.19, internal advisory INT-2026-0107, and "
        "axios, INT-2026-0071), so the AppSec team has no review items for them and the queue's audit reports them "
        "as vulnerable dependencies without review. The findings exist in Dependency-Track, the release's policy "
        "violations exist and reached the violation-tracker, and other releases before and after were reviewed "
        "normally. Cause: upstream Dependency-Track 5.3.0-SNAPSHOT defect in dt-api-server's BOM import "
        f"(ImportBomActivity, activity import-bom). The release was being imported when {rollout} terminated the "
        "old pod: the import had already committed the project's components (processBom's transaction) but had not "
        "yet created its analyze-project run; the engine shutdown interrupted it and abandoned the task (DEBUG "
        "only), and the new pod executed the import again from the start. ImportBomActivity knows which components "
        "are new only through the transient in-memory Component.isNew flag set when it inserts them; the "
        "re-execution found every component already present, so none was new, it stored no vulnerability-analysis "
        "context (the list of new component ids) and created the analyze-project run without one. "
        "NEW_VULNERABLE_DEPENDENCY is emitted only from that context (ReconcileVulnAnalysisResultsActivity), so the "
        "analysis found the vulnerabilities, evaluated the policies and sent POLICY_VIOLATION, but emitted no "
        "NEW_VULNERABLE_DEPENDENCY. Observable traces: the import-bom run of the upload spans the rollout and the "
        "new pod logged the release's import right after it started, although CI had uploaded before the rollout; "
        "the analyze-project run's argument has no context file metadata, unlike other uploads' runs; the first "
        "execution's context file is left unreferenced in SeaweedFS. Nothing re-emits it: later uploads and "
        "analyses see the same, no longer new components. Fix: make those components new again in a fresh import "
        "(delete the release's vulnerable components and upload its BOM again, e.g. export it and PUT /api/v1/bom, "
        "or re-run the release pipeline), so the analysis emits the notifications. Re-analysis, restarts, "
        "re-running the pipeline alone or waiting do nothing. Not the cause: the notification being lost while "
        "being published or delivered during the rollout (it was never created; there is no publish-notification "
        "run for it), the review queue, its signing secret or the dependency-review rule (other releases arrive), "
        "the analysis or the advisories (the findings and violations exist), the CI upload, or Dependency-Track "
        "treating the dependencies as not new because earlier versions of the service already contained them "
        "(each project version's components are new to it, and earlier versions were reviewed).\n\n"
        "Grading requirements. Required for D2-Q1 (core mechanism; an expert must state it or an observable "
        "equivalent): (1) the release's BOM import was interrupted by the api-server rollout after it had already "
        "stored the release's components and was executed again (by the new pod), i.e. the import ran twice "
        "(observable equivalent: the import was redone by the new pod after the rollout, or the import-bom activity "
        "was retried/re-delivered after the components were committed); and (2) because the components already "
        "existed on that second execution, the import no longer treated them as new, so the analysis was started "
        "without the new-components information (no context file) and NEW_VULNERABLE_DEPENDENCY, which only "
        "covers components the import itself marked new, was never emitted, and nothing will emit it later. "
        "Graded credit, not required (D2-Q2): the transient Component.isNew flag, the context file "
        "vuln-analysis/context/<token>.proto and its orphaned copy in SeaweedFS, ReconcileVulnAnalysisResultsActivity "
        "as the only producer, the createRun after the commit, the DEBUG-only abandon with redelivery, the at-least-once "
        "activity contract. D2-Q1 and D2-Q3 must be No for these plausible but wrong mechanisms: the notification "
        "lost in the outbox, the publisher or the webhook delivery because the pod was restarted; an in-memory "
        "notification or event queue lost at shutdown (the notification was never created, not lost after "
        "creation); the review queue, its signature check, the rule's scope or filter; the analyzer, the advisories "
        "or the analysis failing; Dependency-Track deduplicating the alert because the dependency existed in earlier "
        "versions or elsewhere in the portfolio; the CI having uploaded a wrong or partial BOM; a generic 'the "
        "rollout interrupted processing' that does not say that the import was redone after its components were "
        "stored and therefore saw no new components. D2-Q3 asks whether the diagnosis avoids a wrong fault type: "
        "answer Yes when it claims none of these wrong mechanisms. Localization: dt-api-server (its BOM import, "
        "import-bom) is correct; the rollout is the trigger, not the defect, and the dependency-review queue, the CI "
        "or the violation-tracker are not the origin."
    )


class DependencyTrackDependencyReviewsMissing(Problem):
    #: (repository, project, version) of the candidate releases, in order.
    RELEASES = (
        ("acme/checkout-web", "checkout-web", "5.13.0"),
        ("acme/mobile-bff", "mobile-bff", "4.5.0"),
        ("acme/checkout-web", "checkout-web", "5.14.0"),
    )
    EARLIER_RELEASE = ("acme/payments-api", "2.15.0")
    LATER_RELEASE = ("acme/notification-worker", "0.20.0")
    ROUTINE_CHANGE = ("apiServer.web.resources.requests.cpu", "1500m")
    INCIDENT_CHANGE = ("apiServer.web.resources.limits.memory", "3Gi")
    FRONTEND_ROLLOUT_AFTER_S = 420
    LATER_RELEASE_AFTER_S = 660

    def __init__(self, inject=True):
        super().__init__(app=DependencyTrack(profile="reviews"))
        self.kubectl = self.app.kubectl
        self.faulty_service = "dt-api-server"
        self.propagation_duration_s = 1500
        #: False: the same timeline with the release imported normally (healthy check).
        self.inject = inject
        self.incident = None
        self.attempts = []
        self.events = []
        self.root_cause = self.build_structured_root_cause(
            component="deployment/dt-api-server", namespace=self.namespace, description=root_cause_text()
        )
        self.diagnosis_oracle = MechanismGatedJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = DependencyTrackDependencyReviewOracle(problem=self)

    # ------------------------------------------------------------------ helpers
    def _note(self, event, **fields):
        entry = {"t": round(time.time(), 1), "event": event, **fields}
        self.events.append(entry)
        print(f"[inject] {json.dumps(entry)}", flush=True)

    def _helm_change(self, key, value, deployment, wait=True):
        overrides = {**self.app.helm_overrides, key: value}
        self.app.helm_upgrade(overrides)
        self._note("helm upgrade", key=key, value=value)
        if wait:
            self._wait_rolled(deployment)

    def _wait_rolled(self, deployment):
        self.app.rollout(deployment)
        if deployment == self.app.API_DEPLOYMENT:
            self.app.wait_api_ready(timeout=900)

    def wait_ci_idle(self, timeout=2400):
        deadline = time.time() + timeout
        quiet = 0
        while time.time() < deadline:
            try:
                state = self.app.ops("pipelines()", timeout=60)
            except RuntimeError:
                state = {"running": True}
            quiet = quiet + 1 if not state.get("queued") and not state.get("running") else 0
            if quiet >= 3:
                return
            time.sleep(10)
        raise RuntimeError("the pipelines did not go idle")

    def push(self, repository, version):
        self.app.ops(f"push_tag({repository!r}, 'refs/tags/v{version}')")
        self._note("release tag pushed", repository=repository, version=version)

    def project_uuid(self, name, version, timeout=600):
        deadline = time.time() + timeout
        while time.time() < deadline:
            uuid = self.app.admin_call("project_uuid", name, version)
            if uuid:
                return uuid
            time.sleep(5)
        raise RuntimeError(f"project {name} {version} was not created")

    def settled(self, uuid, timeout=1200):
        deadline = time.time() + timeout
        runs = None
        while time.time() < deadline:
            try:
                runs = self.app.admin_call("workflow_runs", uuid, None, 100, timeout=300)
            except RuntimeError as error:  # the API may be restarting
                runs = {"error": str(error)[-300:]}
            items = runs.get("items", []) if isinstance(runs, dict) else []
            names = {r["workflow_name"] for r in items}
            if {"import-bom", "analyze-project"} <= names and all(
                r["status"] in ("COMPLETED", "FAILED", "CANCELLED") for r in items
            ):
                return items
            time.sleep(10)
        raise RuntimeError(f"the release's import and analysis did not settle: {json.dumps(runs)[:800]}")

    def _api_pods(self):
        pods = json.loads(
            self.app.command("get", "pods", "-l", "app.kubernetes.io/component=api-server", "-o", "json")
        )["items"]
        return {p["metadata"]["name"] for p in pods}

    def _analysis_runs(self, session, name, version):
        return int(
            session.run(
                "SELECT count(*) FROM dex_workflow_run r JOIN \"PROJECT\" p ON r.labels ->> 'project_uuid' = "
                f"p.\"UUID\"::text WHERE r.workflow_name = 'analyze-project' AND p.\"NAME\" = '{name}' "
                f"AND p.\"VERSION\" = '{version}'"
            ).strip()
        )

    def _components(self, session, name, version):
        return int(
            session.run(
                'SELECT count(*) FROM "COMPONENT" c JOIN "PROJECT" p ON p."ID" = c."PROJECT_ID" '
                f"WHERE p.\"NAME\" = '{name}' AND p.\"VERSION\" = '{version}'"
            ).strip()
        )

    # ------------------------------------------------------------------ the release across the rollout
    def _release_across_rollout(self, repository, name, version):
        outcome = {"release": f"{name}:{version}"}
        monitor, lock = PsqlSession(self.namespace), PsqlSession(self.namespace)
        locked = False
        try:
            if int(monitor.run(IMPORT_RUNNING).strip()):
                raise RuntimeError("an import is already running")
            lock.run("SELECT 1")
            self.push(repository, version)
            pushed = time.time()
            while not int(monitor.run(IMPORT_RUNNING).strip()):
                if time.time() - pushed > 900:
                    raise RuntimeError("the release's import never started")
                time.sleep(0.05)
            outcome["import_started_after_push_s"] = round(time.time() - pushed, 1)
            if self.inject:
                lock.run("BEGIN; LOCK TABLE dex_workflow_run IN SHARE ROW EXCLUSIVE MODE")
                locked = time.time()
                while not int(monitor.run(BLOCKED_ON_RUNS).strip()):
                    if time.time() - locked > 120:
                        raise RuntimeError("the import never reached its analysis run")
                    time.sleep(0.1)
                outcome["blocked_after_lock_s"] = round(time.time() - locked, 1)
                outcome["components_committed"] = self._components(monitor, name, version)
                outcome["analysis_runs_before"] = self._analysis_runs(monitor, name, version)
                if outcome["components_committed"] and not outcome["analysis_runs_before"]:
                    pods = self._api_pods()
                    rollout = time.time()
                    self._helm_change(*self.INCIDENT_CHANGE, self.app.API_DEPLOYMENT, wait=False)
                    outcome["rollout_at"] = rollout
                    # Hold until the old pod's import can no longer create its run: its waiting call was
                    # interrupted, or the pod is gone (a waiting backend does not notice a dead client).
                    checked = 0.0
                    while int(monitor.run(BLOCKED_ON_RUNS).strip()):
                        if time.time() - checked > 2:
                            checked = time.time()
                            if not pods & self._api_pods() or time.time() - rollout > 200:
                                break
                        time.sleep(0.1)
                    outcome["released_after_s"] = round(time.time() - rollout, 1)
                    outcome["old_pods"] = sorted(pods)
                lock.close(rollback=True)
                outcome["held_s"] = round(time.time() - locked, 1)
                locked = False
        finally:
            if locked:
                lock.close(rollback=True)
            else:
                lock.close()
            monitor.close()
        if "rollout_at" in outcome:
            self._wait_rolled(self.app.API_DEPLOYMENT)
        uuid = self.project_uuid(name, version)
        runs = self.settled(uuid)
        time.sleep(60)  # notifications are published within seconds of the analysis
        findings = self.app.admin_call("findings", uuid, timeout=300)
        reviews = self.app.ops(f"reviews({uuid!r})", timeout=120)
        violations = self.app.admin_call("violations", uuid, timeout=300)
        tickets = {t["violation_uuid"] for t in self.app.ops(f"tickets({uuid!r})", timeout=120)}
        outcome.update(
            project_uuid=uuid,
            runs={r["workflow_name"]: r["status"] for r in sorted(runs, key=lambda r: r["created_at"])},
            imports=len([r for r in runs if r["workflow_name"] == "import-bom"]),
            findings=sorted(f"{f['component']} {f['vulnerability']}" for f in findings),
            reviews=len(reviews),
            violations=len(violations),
            tickets_missing=len([v for v in violations if v["uuid"] not in tickets]),
        )
        outcome["lost"] = bool(
            "rollout_at" in outcome
            and findings
            and not reviews
            and outcome["runs"].get("analyze-project") == "COMPLETED"
            and not outcome["tickets_missing"]
        )
        outcome["finding_records"] = findings
        return outcome

    def _update_root_cause(self):
        incident = self.incident
        rollout = time.strftime("the api-server rollout at %H:%M UTC", time.gmtime(incident["rollout_at"]))
        words = {1: "one", 2: "two", 3: "three", 4: "four"}
        count = words.get(len(incident["finding_components"]), str(len(incident["finding_components"])))
        self.root_cause = self.build_structured_root_cause(
            component="deployment/dt-api-server",
            namespace=self.namespace,
            description=root_cause_text(f"{incident['project']} {incident['version']}", rollout, count),
        )
        self.diagnosis_oracle.expected = self.root_cause

    # ------------------------------------------------------------------ injection
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: an api-server rollout while a release is imported ==")
        self.wait_ci_idle()
        self._helm_change(*self.ROUTINE_CHANGE, self.app.API_DEPLOYMENT)
        time.sleep(120)
        self.push(*self.EARLIER_RELEASE)
        self.wait_ci_idle()
        time.sleep(180)
        for repository, name, version in self.RELEASES:
            outcome = self._release_across_rollout(repository, name, version)
            record = {k: v for k, v in outcome.items() if k != "finding_records"}
            self.attempts.append(record)
            self._note("release outcome", **record)
            self.wait_ci_idle()
            if outcome["lost"] or (not self.inject and outcome["reviews"]):
                break
            if "rollout_at" in outcome:
                raise RuntimeError(f"the release imported across the rollout kept its alerts: {json.dumps(record)}")
            time.sleep(60)
        else:
            raise RuntimeError(f"no release could be imported across the rollout: {json.dumps(self.attempts)[-3000:]}")
        at = outcome.get("rollout_at") or time.time()
        if not self.inject:
            # Healthy check: the same configuration change, after the import.
            self._helm_change(*self.INCIDENT_CHANGE, self.app.API_DEPLOYMENT)
        findings = outcome["finding_records"]
        self.incident = {
            "project": name,
            "version": version,
            "project_uuid": outcome["project_uuid"],
            "rollout_at": at,
            "components": self.app.admin_call("component_count", outcome["project_uuid"], timeout=120),
            "advisories": sorted({f["vulnerability"] for f in findings}),
            "finding_components": sorted({f["component_uuid"] for f in findings}),
        }
        if self.inject:
            self._update_root_cause()
        while time.time() < at + self.FRONTEND_ROLLOUT_AFTER_S:
            time.sleep(10)
        self._helm_change("frontend.resources.limits.memory", "192Mi", self.app.FRONTEND_DEPLOYMENT)
        while time.time() < at + self.LATER_RELEASE_AFTER_S:
            time.sleep(10)
        self.push(*self.LATER_RELEASE)
        self.wait_ci_idle()
        self.incident["handover_ready_at"] = time.time()
        self.incident["handover_notifications"] = sorted(
            {r["notification_id"] for r in self.app.ops(f"reviews({outcome['project_uuid']!r})", timeout=120)}
        )
        self.mitigation_oracle.note_injected_configuration()

    # ------------------------------------------------------------------ recovery
    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: import the release's vulnerable components again as new components ==")
        if not self.incident:
            return
        uuid = self.incident["project_uuid"]
        result = self.app.admin_call("reimport_components", uuid, as_admin=True, timeout=900)
        self._note("vulnerable components re-imported", **result)
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(20)
            findings = [f for f in self.app.admin_call("findings", uuid, timeout=300) if not f["suppressed"]]
            reviewed = {(r["component_uuid"], r["vulnerability"]) for r in self.app.ops(f"reviews({uuid!r})")}
            if findings and all((f["component_uuid"], f["vulnerability"]) in reviewed for f in findings):
                return
        raise RuntimeError("the release's vulnerable dependencies did not reach the review queue after the re-import")
