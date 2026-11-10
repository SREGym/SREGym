"""Dependency-Track: a release's vulnerable dependency never reaches the intake register.

Upstream defect (Dependency-Track 5.3.0-SNAPSHOT at fd55decc, unreported; found
by model checking and reproduced here on the published image): BOM imports
(concurrency key ``import-bom:<project>``) and analyses (``analyze-project:`` /
``vuln-analysis:<project>``) of one project are not serialized against each
other. ``PrepareVulnAnalysisActivity`` snapshots the project's components into
the analysis BOM with their database ids as bom-refs;
``ReconcileVulnAnalysisResultsActivity`` turns the analyzers' results back into
those ids and inserts ``COMPONENTS_VULNERABILITIES`` / ``FINDINGATTRIBUTION``
rows without checking that the components still exist. When a later import of
the same project deletes one of the snapshotted vulnerable components before the
reconcile transaction commits, the COMMIT fails on the ``DEFERRABLE INITIALLY
DEFERRED`` component foreign key (SQLSTATE 23503). Every retry replays the same
frozen results, so all 6 attempts fail; ``vuln-analysis`` and
``analyze-project`` of the first upload end FAILED (logged at DEBUG only) and
its analysis context file, which lists the components that upload created, is
deleted. NEW_VULNERABLE_DEPENDENCY is emitted only from that context (the
components an import created), so it is never sent for the first upload's new
vulnerable components. The second upload's analysis restores the findings,
violations and metrics, but its context only lists the components the second
import created, and manual or scheduled analyses carry no context at all.

The natural event here: checkout-web 5.13.0 is tagged, and about a minute later
the dependency update bot merges its security update of micromatch (6.27.19,
internal advisory INT-2026-0107, to 6.28.1) to main. The release pipeline
uploads 5.13.0's SBOM; the main pipeline (an incremental build, so it finishes
quickly) uploads its SBOM under the same version about 15 s later. Its import
replaces micromatch 6.27.19 while the release upload's analysis is reconciling,
so that analysis fails, and the release's new vulnerable dependency moment
7.9.19 (internal advisory INT-2026-0114, HIGH) never gets its
NEW_VULNERABLE_DEPENDENCY notification: the violation-tracker's intake register
has no intake ticket for it, and its audit reports a vulnerable dependency
without an intake ticket. The finding and its policy violations are present
(the main upload's analysis created them; their tickets exist). An ordinary
release and a bot merge of another service precede the incident; a routine
api-server rollout (which also replaces the pod whose log had the reconcile
WARNs) and another release follow it.

Driver (deterministic, invisible at handover): as soon as the release upload's
import has committed, the conductor opens two short PostgreSQL sessions. One
holds ``FOR KEY SHARE`` on micromatch 6.27.19's COMPONENT row, so the main
import cannot delete it before the release analysis has taken its snapshot; the
other inserts, uncommitted, the release's (moment, INT-2026-0114) finding row,
so the reconcile transaction's first INSERT waits on it (no row locks taken
yet: the foreign keys are deferred). Once the reconcile waits, the first
session rolls back; once the main import has committed (micromatch 6.27.19
gone), the second rolls back, about 10-25 s after it began (below the 60 s
statement timeout), and the reconcile commits into the foreign key failure.
This is the order a slower analysis (more components, a remote analyzer)
produces by itself. The sessions are gone at handover; the postgres log keeps
the reconcile's FK errors, as it would.

Reference mitigation (needs the mechanism): make the dependency new to an
import of the release again, so an import's analysis emits the notification:
delete the moment 7.9.19 component from checkout-web 5.13.0
(``DELETE /api/v1/component/{uuid}``) and re-run the release pipeline (push the
tag again) or re-upload the release SBOM; or upload the release SBOM without the
dependency and then with it. Re-analysis, re-running the pipeline without
removing the dependency, restarts and waiting do nothing; deleting the project,
hand-posting intake tickets or webhooks, or changing policies, rules or
advisories are rejected; intake tickets from hand-made notifications (a signed
webhook, or a row written into the notification outbox) do not count.
"""

import calendar
import contextlib
import json
import secrets
import subprocess
import time

from sregym.conductor.oracles.apps.dependency_track.dependency_intake import DependencyTrackIntakeOracle
from sregym.conductor.oracles.apps.dependency_track.diagnosis import MechanismGatedJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.dependency_track.application import DependencyTrack
from sregym.utils.decorators import mark_fault_injected


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


def root_cause_text(release="checkout-web 5.13.0", main_upload="the main pipeline's upload about 15 s later"):
    return (
        f"The violation-tracker's intake register has no intake ticket for {release}'s vulnerable dependency moment "
        "7.9.19 (internal advisory INT-2026-0114, HIGH), and its audit reports it as a vulnerable dependency without "
        "an intake ticket, although the finding and its policy violations exist (with tickets): Dependency-Track never "
        "sent the NEW_VULNERABLE_DEPENDENCY notification for it. Cause: upstream Dependency-Track 5.3.0-SNAPSHOT race "
        "in dt-api-server between BOM import and vulnerability analysis of the same project: they are not serialized "
        "against each other (concurrency keys import-bom:<project> vs analyze-project:/vuln-analysis:<project>). The "
        f"release tag's upload of {release} was being analysed when the main-branch pipeline uploaded an SBOM under "
        f"the same project version ({main_upload}), after the dependency update bot merged the micromatch 6.27.19 -> "
        "6.28.1 update. That import deleted the micromatch 6.27.19 component, which the release analysis had "
        "snapshotted (PrepareVulnAnalysisActivity, component database ids as bom-refs) and found vulnerable "
        "(INT-2026-0107). ReconcileVulnAnalysisResultsActivity then inserted findings for the deleted component id; "
        "the deferred foreign key COMPONENTS_VULNERABILITIES_COMPONENT_FK failed the COMMIT (SQLSTATE 23503, 'Key "
        "(COMPONENT_ID)=(...) is not present in table COMPONENT'), and every one of the 6 attempts replayed the same "
        "results and failed. The release upload's vuln-analysis and analyze-project runs ended FAILED (token FAILED; "
        "the workflow failure is only logged at DEBUG, the activity WARNs were in the replaced api-server pod's log) "
        "and its analysis context, the list of components that import created (all of the new version's components, "
        "moment included), was dropped, so the NEW_VULNERABLE_DEPENDENCY notifications of that upload were never "
        "emitted. The main upload's analysis then created the findings and violations (so the PV tickets exist), but "
        "NEW_VULNERABLE_DEPENDENCY is only emitted for components the triggering import itself created, and moment "
        "already existed for it; re-analyses carry no context, so nothing ever sends it. Fix: make moment 7.9.19 new "
        "to an import of the release again, so that import's analysis emits the notification: delete the component "
        "from checkout-web 5.13.0 and re-run the release pipeline or re-upload the release SBOM (or upload the SBOM "
        "without moment, then with it). Re-analysis or re-uploading the same SBOM do not emit it. Not the cause: the "
        "api-server rollout afterwards (the analysis had failed and the main upload's analysis had completed before "
        "it), webhook delivery or the violation-tracker (no such notification was ever emitted), the notification "
        "rule, the advisory or the policies, or the analyzer.\n\n"
        "Grading requirements. Required for D2-Q1 (core mechanism; an expert must state it or an observable "
        "equivalent): (1) the release upload's vulnerability analysis failed because the second, overlapping upload "
        "of the same project version (the main pipeline's) deleted or replaced a component (micromatch 6.27.19) that "
        "the running analysis had already picked up and found vulnerable, so its results referenced a component that "
        "no longer existed and the reconcile commit failed on the component foreign key on every retry (observable "
        "equivalent: the first analyze-project/vuln-analysis run FAILED with the COMPONENT_ID foreign key error for "
        "the id of the component the second import removed); and (2) because that analysis failed, the release "
        "upload's NEW_VULNERABLE_DEPENDENCY notifications were never emitted, and the second upload's analysis does "
        "not emit one for moment because the dependency was not new to that import (re-analysis does not either). "
        "Graded credit, not required (D2-Q2): the missing serialization between the import-bom and "
        "analyze-project/vuln-analysis concurrency keys, the component database ids in the analysis BOM, the deferred "
        "foreign key failing at COMMIT, the frozen activity input replayed by the 6 retries, the context file listing "
        "the import's new components and its deletion on failure, the DEBUG-only workflow failure. D2-Q1 and D2-Q3 "
        "must be No for these plausible but wrong mechanisms: the api-server rollout killing the analysis or losing "
        "an in-flight notification; a webhook delivery failure or the violation-tracker dropping or rejecting the "
        "notification; a notification rule, advisory or policy misconfiguration; Dependency-Track treating moment "
        "as already known because earlier checkout-web versions or the portfolio contain it; the second upload "
        "merely superseding, cancelling or deduplicating the first upload's analysis without the deleted-component "
        "failure; a database outage, lock or corruption causing the foreign key error. D2-Q3 asks whether the "
        "diagnosis avoids a wrong fault type: answer Yes when it claims none of these wrong mechanisms (mentioning "
        "the rollout or the tracker as ruled out is fine). Localization: dt-api-server (its BOM import / "
        "vulnerability analysis workflows) is correct; the overlapping main-pipeline upload from ci-pipelines is the "
        "trigger, not the defect, and the violation-tracker is not the origin."
    )


class DependencyTrackReleaseIntakeMissing(Problem):
    RELEASE = ("acme/checkout-web", "checkout-web", "5.13.0")
    DEPENDENCY = "pkg:npm/moment@7.9.19"
    ADVISORY = "INT-2026-0114"
    UPDATED = ("micromatch", "6.27.19", "6.28.1")
    UPDATE_MESSAGE = "fix(deps): update dependency micromatch to v6.28.1 [security]"
    #: An ordinary release and a bot merge of another service before the incident.
    EARLIER_RELEASE = ("acme/payments-api", "2.15.0")
    EARLIER_MERGE = (
        "acme/payments-api",
        "chore(deps): update dependency com.fasterxml.jackson.core:jackson-databind to v2.18.4",
    )
    LATER_RELEASE = ("acme/notification-worker", "0.20.0")
    API_ROLLOUT_AFTER_S = 420
    LATER_RELEASE_AFTER_S = 720
    #: The reconcile statement must not wait longer than this (dt.datasource.query-timeout-ms is 60 s).
    MAX_BLOCK_S = 50

    def __init__(self):
        super().__init__(app=DependencyTrack(profile="intake"))
        self.kubectl = self.app.kubectl
        self.faulty_service = "dt-api-server"
        self.propagation_duration_s = 600
        self.incident = None
        self.attempts = []
        self.events = []
        self.root_cause = self.build_structured_root_cause(
            component="deployment/dt-api-server", namespace=self.namespace, description=root_cause_text()
        )
        self.diagnosis_oracle = MechanismGatedJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = DependencyTrackIntakeOracle(problem=self)

    # ------------------------------------------------------------------ helpers
    def _note(self, event, **fields):
        entry = {"t": round(time.time(), 1), "event": event, **fields}
        self.events.append(entry)
        print(f"[inject] {json.dumps(entry)}", flush=True)

    def _helm_change(self, key, value, deployment):
        overrides = {**self.app.helm_overrides, key: value}
        self.app.helm_upgrade(overrides)
        self.app.rollout(deployment)
        if deployment == self.app.API_DEPLOYMENT:
            self.app.wait_api_ready()
        self._note("helm upgrade", key=key, value=value)

    def wait_ci_idle(self, timeout=2400):
        deadline = time.time() + timeout
        quiet = 0
        while time.time() < deadline:
            state = self.app.ops("pipelines()", timeout=60)
            quiet = quiet + 1 if not state.get("queued") and not state.get("running") else 0
            if quiet >= 3:
                return
            time.sleep(10)
        raise RuntimeError("the pipelines did not go idle")

    def push(self, repository, version):
        self.app.ops(f"push_tag({repository!r}, 'refs/tags/v{version}')")
        self._note("release tag pushed", repository=repository, version=version)

    def merge(self, repository, message):
        commits = [{"id": secrets.token_hex(20), "message": message, "author": "renovate[bot]"}]
        self.app.ops(f"push_main({repository!r}, {commits!r})")
        self._note("main push", repository=repository, message=message)

    def project_uuid(self, name, version, timeout=600):
        deadline = time.time() + timeout
        while time.time() < deadline:
            uuid = self.app.admin_call("project_uuid", name, version)
            if uuid:
                return uuid
            time.sleep(5)
        raise RuntimeError(f"project {name} {version} was not created")

    def runs(self, uuid, name=None):
        runs = self.app.admin_call("workflow_runs", uuid, name, 100, timeout=300)
        return sorted(runs.get("items", []) if isinstance(runs, dict) else [], key=lambda r: r["created_at"])

    def ci_log_since(self, since_s, needle):
        out = self.app.command("logs", f"deploy/{self.app.CI}", f"--since={max(1, int(since_s))}s", timeout=60)
        return [line for line in out.splitlines() if needle(line)]

    def intake_for(self, uuid):
        return [t for t in self.app.ops(f"intake({uuid!r})", timeout=120) if t["purl"] == self.DEPENDENCY]

    # ------------------------------------------------------------------ the release and the bot merge
    def _release_with_update(self):
        repository, name, version = self.RELEASE
        dependency, (updated, old, _new) = self.DEPENDENCY, self.UPDATED
        old_purl = f"pkg:npm/{updated}@{old}"
        vuln = int(self.app.psql(f'SELECT "ID" FROM "VULNERABILITY" WHERE "VULNID" = \'{self.ADVISORY}\'').strip())
        outcome = {"release": f"{name}:{version}"}
        watch, hold, block = (PsqlSession(self.namespace) for _ in range(3))
        try:
            block_pid = int(block.run("SELECT pg_backend_pid()").strip())
            self.push(repository, version)
            pushed = time.time()
            while not self.ci_log_since(
                time.time() - pushed + 5,
                lambda line: '"build finished"' in line and f'"pipeline": "{name}"' in line and '"tag"' in line,
            ):
                if time.time() - pushed > 600:
                    raise RuntimeError("the release build did not finish")
                time.sleep(1)
            outcome["release_build_s"] = round(time.time() - pushed, 1)
            self.merge(repository, self.UPDATE_MESSAGE)
            merged = time.time()
            ids = {}
            while len(ids) < 2:
                out = watch.run(
                    'SELECT c."PURL", c."ID" FROM "COMPONENT" c JOIN "PROJECT" p ON p."ID" = c."PROJECT_ID" '
                    f"WHERE p.\"NAME\" = '{name}' AND p.\"VERSION\" = '{version}' "
                    f"AND c.\"PURL\" IN ('{dependency}', '{old_purl}')"
                )
                ids = dict(line.split("|") for line in out.splitlines() if "|" in line)
                if time.time() - merged > 300:
                    raise RuntimeError(f"the release import did not commit: {ids}")
                if len(ids) < 2:
                    time.sleep(0.1)
            hold.run(f'BEGIN; SELECT 1 FROM "COMPONENT" WHERE "ID" = {ids[old_purl]} FOR KEY SHARE')
            block.run(
                'BEGIN; INSERT INTO "COMPONENTS_VULNERABILITIES" ("COMPONENT_ID", "VULNERABILITY_ID") '
                f"VALUES ({ids[dependency]}, {vuln})"
            )
            blocked_from = time.time()
            outcome.update(
                imported_after_merge_s=round(blocked_from - merged, 1),
                component_ids={dependency: int(ids[dependency]), old_purl: int(ids[old_purl])},
            )
            if int(ids[dependency]) > int(ids[old_purl]):
                outcome["warning"] = "dependency id above the replaced component's id"
            while not int(
                watch.run(f"SELECT count(*) FROM pg_stat_activity WHERE {block_pid} = ANY(pg_blocking_pids(pid))")
            ):
                if time.time() - blocked_from > 120:
                    raise RuntimeError("the release analysis never reached its reconciliation")
                time.sleep(0.2)
            outcome["reconcile_waiting_after_s"] = round(time.time() - blocked_from, 1)
            hold.close(rollback=True)
            while int(watch.run(f'SELECT count(*) FROM "COMPONENT" WHERE "ID" = {ids[old_purl]}').strip()):
                if time.time() - blocked_from > self.MAX_BLOCK_S:
                    outcome["main_import_late"] = True
                    break
                time.sleep(0.2)
            outcome["reconcile_released_after_s"] = round(time.time() - blocked_from, 1)
        finally:
            block.close(rollback=True)
            hold.close(rollback=True)
            watch.close()
        released = time.time()
        uuid = self.project_uuid(name, version)
        deadline = time.time() + 900
        while time.time() < deadline:
            analyses = self.runs(uuid, "analyze-project")
            if len(analyses) >= 2 and all(r["status"] in ("COMPLETED", "FAILED", "CANCELLED") for r in analyses):
                break
            time.sleep(5)
        time.sleep(30)
        analyses = [r["status"] for r in self.runs(uuid, "analyze-project")]
        vuln_analyses = [r["status"] for r in self.runs(uuid, "vuln-analysis")]
        findings = {(f["component"], f["vulnerability"]) for f in self.app.admin_call("findings", uuid)}
        outcome.update(
            project_uuid=uuid,
            settled_after_s=round(time.time() - released, 1),
            analyze_project=analyses,
            vuln_analysis=vuln_analyses,
            finding=(dependency, self.ADVISORY) in findings,
            intake=len(self.intake_for(uuid)),
            release_upload_at=pushed + outcome["release_build_s"],
            main_upload_at=merged + 15,
        )
        uploads = self.upload_times(time.time() - pushed + 30)
        for trigger, key in (("tag", "release_upload_at"), ("push", "main_upload_at")):
            stamps = [stamp for kind, stamp in uploads if kind == trigger]
            if stamps:
                outcome[key] = stamps[0]
        outcome["triggered"] = bool(
            analyses[:1] == ["FAILED"] and "COMPLETED" in analyses[1:] and outcome["finding"] and not outcome["intake"]
        )
        return outcome

    def upload_times(self, since_s):
        """CI's 'sbom uploaded' times of the release version: [(trigger, epoch seconds)]."""
        _, name, version = self.RELEASE
        found = []
        for line in self.ci_log_since(since_s, lambda line: '"sbom uploaded"' in line):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("pipeline") == name and record.get("version") == version:
                stamp = calendar.timegm(time.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ"))
                found.append((record.get("trigger"), stamp))
        return found

    def _update_root_cause(self):
        gap = round(self.incident["main_upload_at"] - self.incident["release_upload_at"])
        when = time.strftime("%H:%M UTC", time.gmtime(self.incident["release_upload_at"]))
        self.root_cause = self.build_structured_root_cause(
            component="deployment/dt-api-server",
            namespace=self.namespace,
            description=root_cause_text(
                f"{self.RELEASE[1]} {self.RELEASE[2]}",
                f"about {max(gap, 5)} s after the release upload at {when}",
            ),
        )
        self.diagnosis_oracle.expected = self.root_cause

    # ------------------------------------------------------------------ injection
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: a main-branch upload overlaps the release upload's analysis ==")
        self.wait_ci_idle()
        self.push(*self.EARLIER_RELEASE)
        self.wait_ci_idle()
        time.sleep(90)
        self.merge(*self.EARLIER_MERGE)
        self.wait_ci_idle()
        time.sleep(150)
        outcome = self._release_with_update()
        self.attempts.append(outcome)
        self._note("release outcome", **outcome)
        if not outcome["triggered"]:
            raise RuntimeError(f"the release's intake notification was not lost: {json.dumps(outcome)}")
        self.wait_ci_idle()
        _, name, version = self.RELEASE
        self.incident = {
            "project": name,
            "version": version,
            "project_uuid": outcome["project_uuid"],
            "dependency": self.DEPENDENCY,
            "advisory": self.ADVISORY,
            "release_upload_at": outcome["release_upload_at"],
            "main_upload_at": outcome["main_upload_at"],
            "rollout_at": outcome["release_upload_at"],
            "components": self.app.admin_call("component_count", outcome["project_uuid"], timeout=120),
            "runs": {"analyze-project": outcome["analyze_project"], "vuln-analysis": outcome["vuln_analysis"]},
        }
        self._update_root_cause()
        while time.time() < self.incident["release_upload_at"] + self.API_ROLLOUT_AFTER_S:
            time.sleep(10)
        self._helm_change("apiServer.web.resources.requests.cpu", "1500m", self.app.API_DEPLOYMENT)
        while time.time() < self.incident["release_upload_at"] + self.LATER_RELEASE_AFTER_S:
            time.sleep(10)
        self.push(*self.LATER_RELEASE)
        self.wait_ci_idle()
        uuid = self.incident["project_uuid"]
        self.incident["dependency_components"] = sorted(
            c["uuid"]
            for c in self.app.admin_call("project_components", uuid, timeout=300)
            if c["purl"] == self.DEPENDENCY
        )
        self.incident["handover_notifications"] = sorted({t["notification_id"] for t in self.intake_for(uuid)})
        self.mitigation_oracle.note_injected_configuration()
        self.incident["handover_at"] = time.time()

    # ------------------------------------------------------------------ recovery
    def remove_dependency(self, uuid):
        """Delete the dependency's component from the release (it is re-created as new by the next import)."""
        removed = []
        for component in self.app.admin_call("project_components", uuid, timeout=300):
            if component["purl"] == self.DEPENDENCY:
                self.app.admin_call("delete_component", component["uuid"], as_admin=True)
                removed.append(component["uuid"])
        return removed

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: re-import the release with the dependency new again ==")
        if not self.incident:
            return
        uuid = self.incident["project_uuid"]
        if self.intake_for(uuid):
            return
        self.wait_ci_idle()
        self.remove_dependency(uuid)
        repository, _, version = self.RELEASE
        self.push(repository, version)
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(15)
            if self.intake_for(uuid):
                return
        raise RuntimeError("the release's dependency did not reach the intake register after re-import")
