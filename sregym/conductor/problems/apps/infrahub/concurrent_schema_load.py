"""Two schema releases loaded 0.3 s apart silently lose an attribute and leave one API worker stale.

Upstream bug (Infrahub 1.11.9, unfixed and unpublished at the time of writing;
found and reproduced by Specula as MC-8): ``POST /api/schema/load`` takes
``lock.registry.global_schema_lock()``, an ``InfrahubMultiLock`` of the
per-process ``local.schema`` asyncio lock and the Redis ``global.schema`` lock,
in that order (``backend/infrahub/api/schema.py``). A second load that arrives
on another gunicorn worker while the first holds ``global.schema`` takes its own
worker's ``local.schema`` and then waits. That also blocks the worker's
``refresh.registry.branches`` handler (``refresh_branches`` needs
``local.schema``), so when the first load commits, the waiting worker never
reloads its in-memory schema registry. It then computes its candidate, inside
the lock, from the stale registry (``evaluate_candidate_schemas``) and writes the
kind's attribute list back from it (``update_node_in_db_based_on_diff``), which
detaches the attribute the first load added. It also saves a schema hash computed
from its stale registry, so that worker never reloads the kinds only the first
load touched (Specula's HashMatchesDB variant). Both requests return HTTP 200.

The natural event is two teams' release pipelines loading their schema files in
the same release window, 0.3 s apart: the asset team adds ``serial_number`` to
``NetworkDevice`` and ``NetworkModule``, the lifecycle team adds
``firmware_version`` to ``NetworkDevice``; then inventory-sync's configuration
starts using the new attributes. The conductor sends both loads from the client
pod over stdin (no Jobs, nothing printed in the cluster). Nothing is injected
into Infrahub, Redis or Neo4j. The outcome required at handover:
``NetworkDevice.serial_number`` is lost on every worker and one API worker still
serves ``NetworkModule`` without ``serial_number``. When the two loads land on
the same worker (no race), the namespace is redeployed from scratch and the
release is retried, so no reset or retry is visible to the agent.

The agent gets the plain application description only. The symptom shows in
inventory-sync's own log (failed device registrations, and module operations
that fail on about one request in four) and in the API's answers.
"""

import logging
import time

from sregym.conductor.oracles.apps.infrahub.schema_release import InfrahubSchemaReleaseOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.infrahub.application import BASE_FIELDS, InfrahubApplication
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.infrahub")

DEVICE, MODULE = "NetworkDevice", "NetworkModule"
#: Schema files of the two release pipelines, in start order: (node, attributes it adds).
ASSET_RELEASE = [("device", ["serial_number"]), ("module", ["serial_number"])]
LIFECYCLE_RELEASE = [("device", ["firmware_version"])]
RELEASED = {DEVICE: ["serial_number", "firmware_version"], MODULE: ["serial_number"]}
RELEASE_GAP_SECONDS = 0.3
MAX_ATTEMPTS = 4
#: The incident ages this long (inventory-sync keeps running) before the agent is handed the cluster.
PROPAGATION_S = 25 * 60

ROOT_CAUSE = (
    "Two schema loads to main (POST /api/schema/load), sent 0.3 s apart by two teams' release pipelines, were "
    "served by different gunicorn workers of infrahub-server. The asset team's file added serial_number to "
    "NetworkDevice and NetworkModule; the lifecycle team's file added firmware_version to NetworkDevice. "
    "Upstream Infrahub 1.11.9 concurrency defect in the schema load path: global_schema_lock() acquires the "
    "worker's in-process local.schema asyncio lock before the Redis global.schema lock, so the second request "
    "held its own worker's local.schema while it waited for the first load. That blocked the same worker's "
    "refresh.registry.branches handler (refresh_branches needs local.schema), so when the first load committed, "
    "the waiting worker never reloaded its in-memory schema registry. When it then got the global lock it "
    "computed its candidate inside the lock but from that stale registry (NetworkDevice without serial_number) "
    "and wrote NetworkDevice's attribute list back from it, detaching the serial_number the first load had just "
    "added. It also saved a schema hash computed from its stale registry, so that one worker never reloads and "
    "keeps serving NetworkModule without serial_number, although the stored NetworkModule schema has it. Both "
    "loads returned HTTP 200. Result: serial_number is gone from the stored NetworkDevice schema on every worker "
    "(inventory-sync's device registrations fail with \"Field 'serial_number' is not defined\"), and one of the "
    "four API workers serves a stale NetworkModule, so about one in four module requests fail. Fix: load the "
    "complete intended schema (both teams' attributes) once, serially, and make every API worker serve it "
    "(restart infrahub-server, or make sure the stale worker reloads), keeping existing data and inventory-sync's "
    "configured fields. "
    "Grading requirements: the mechanism is required for fault characterization (D2). Infrahub schema loads are "
    "additive (attributes a file does not list are kept), so a diagnosis that blames full-node or replace "
    "semantics of the lifecycle team's file (the second file did not list serial_number, so loading it removed "
    "the attribute) states the wrong mechanism. A diagnosis that says the candidate or diff was computed before "
    "or outside the lock, or that the loads were simply not locked or serialized, also states the wrong "
    "mechanism: the global lock was held and serialized the two loads; the candidate was computed inside it from "
    "a per-worker registry that the worker's own held local.schema lock had kept from refreshing. A diagnosis "
    "that only describes two concurrent loads causing a lost update from a stale base, without the second "
    "worker's in-memory registry missing the refresh because of its own held local schema lock, does not "
    "identify the mechanism. In any of these cases answer No to D2-Q1, D2-Q2 and D2-Q3."
)


class InfrahubConcurrentSchemaLoadLostAttribute(Problem):
    def __init__(self):
        app = InfrahubApplication()
        super().__init__(app)
        self.kubectl = app.kubectl
        self.faulty_service = "infrahub-server"
        self.observed = {}
        self.releases_started = False
        self.propagation_duration_s = PROPAGATION_S
        self.root_cause = self.build_structured_root_cause(
            component="infrahub-server", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = InfrahubSchemaReleaseOracle(self, BASE_FIELDS, RELEASED)
        app.describe()

    # ------------------------------------------------------------------ helpers
    def _outcome(self, releases, views):
        """The race outcome this problem needs: serial_number lost everywhere, one worker stale on modules."""
        devices = [set(view.split(",")) for view in views[DEVICE]]
        modules = {view: count for view, count in views[MODULE].items()}
        stale = sum(c for v, c in modules.items() if "serial_number" not in v.split(","))
        return (
            all(r and r["status"] == 200 for r in releases)
            and all("serial_number" not in s and "firmware_version" in s for s in devices)
            and 0 < stale < sum(modules.values())
        )

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                # A clean slate: an in-place reset would leave extra schema loads in the logs.
                app.redeploy()
                self.mitigation_oracle.capture_baseline()
            self.releases_started = True
            releases = app.run_client(
                "release", files=[ASSET_RELEASE, LIFECYCLE_RELEASE], gap=RELEASE_GAP_SECONDS, exec_timeout=900
            )
            time.sleep(15)  # every worker processes the refresh broadcasts of both loads
            views = app.run_client("views", kinds=[DEVICE, MODULE], samples=32, exec_timeout=600)
            record = {"releases": releases, "views": views}
            self.observed[f"attempt_{attempt}"] = record
            logger.info("Schema release attempt %d: %s", attempt, record)
            if self._outcome(releases, views):
                self.observed.update(attempts=attempt, released_at=releases[0]["started"])
                # The release continues: inventory-sync starts populating the new attributes.
                app.set_inventory_fields(DEVICE, BASE_FIELDS[DEVICE] + RELEASED[DEVICE])
                app.set_inventory_fields(MODULE, BASE_FIELDS[MODULE] + RELEASED[MODULE])
                return
            self.releases_started = False
        raise RuntimeError(f"Concurrent schema releases never produced the lost attribute: {self.observed}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference mitigation: one serial load of the full schema, then make every API worker serve it."""
        if self.mitigation_oracle.evaluate().get("success"):
            return
        app = self.app
        result = app.run_client(
            "load_schema", extra=RELEASED[DEVICE], module_extra=RELEASED[MODULE], timeout=600, exec_timeout=900
        )
        if result["status"] != 200:
            raise RuntimeError(f"Full schema load failed: {result}")
        expected = {kind: ["name", *BASE_FIELDS[kind], *RELEASED[kind]] for kind in (DEVICE, MODULE)}
        if not app.run_client("wait_views", expected=expected, timeout=120, exec_timeout=600)["converged"]:
            app.command("rollout", "restart", "deployment/infrahub-server")
            app.rollout("infrahub-server", timeout=600)
            app.wait_workers_registered()
            if not app.run_client("wait_views", expected=expected, timeout=180, exec_timeout=600)["converged"]:
                raise RuntimeError("API workers do not serve the full schema")
        for kind in (DEVICE, MODULE):
            app.set_inventory_fields(kind, BASE_FIELDS[kind] + RELEASED[kind])
