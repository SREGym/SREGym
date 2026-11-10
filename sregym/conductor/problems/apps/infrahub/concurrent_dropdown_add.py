"""Two teams add Dropdown choices to NetworkDevice at the same moment; one acknowledged choice is lost.

Upstream bug (Infrahub 1.11.9, unfixed and unpublished at the time of writing;
found and reproduced by Specula as CR-11): the GraphQL schema mutations
``SchemaDropdownAdd``, ``SchemaDropdownRemove``, ``SchemaEnumAdd`` and
``SchemaEnumRemove`` (``backend/infrahub/graphql/mutations/schema.py``) copy the
whole node schema from the worker's in-memory registry (``db.schema.get(...,
duplicate=True)``) *before* any lock, change one attribute of the copy, and pass
the whole node to ``update_registry``. Only ``update_registry`` takes
``lock.registry.global_schema_lock()``; inside it the copy replaces the kind
wholesale (``tmp_schema.set(name=kind.kind, schema=kind)``) and every attribute
of the node is written back to Neo4j. Nothing checks that the copy is still the
state it overwrites. Two overlapping mutations on one kind are serialized by the
lock, but the second writes back a whole-node copy taken before the first
committed, so the first change is reverted. Both return ``ok: true``.

The natural event: two teams change the NetworkDevice schema in the same few
seconds with ordinary ``SchemaDropdownAdd`` calls, the data-center team adding
site ``dc4`` (a new data center) and the lifecycle team adding status ``rma``,
0.3 s apart. Then inventory-sync's configuration starts using both values
(the dc4 rollout and RMA tracking go live). The conductor sends both mutations
from the client pod (an ordinary API client; nothing stored in the cluster).
Nothing is injected into Infrahub, Redis or Neo4j. The outcome required at
handover: every API worker serves the same NetworkDevice schema, with ``rma``
and without ``dc4`` (the stored schema lacks it, so no worker is stale). When
the race does not produce that, the namespace is redeployed from scratch and
the changes are retried, so no reset or retry is visible to the agent. The
incident ages 25 minutes (inventory-sync keeps working) before handover.

The agent gets the plain application description only. The symptom shows in
inventory-sync's own log: every registration of a dc4 device fails with
"dc4 must be one of 'dc1, dc2, dc3' at site", while rma devices register.

How it differs from infrahub_concurrent_schema_load_lost_attribute (MC-8, same
code family): no REST schema load, no stale API worker, and nothing a restart
or reload could fix. Here the stored schema itself lacks the choice, every
worker agrees, and the copy was taken before the lock (on one worker or two);
MC-8's blocked registry refresh is at most an accepted detail. The repair is
to add the lost choice again (a Dropdown mutation or a schema load carrying the
complete choice lists), not to reload a worker.
"""

import json
import logging
import time

from sregym.conductor.oracles.apps.infrahub.dropdown_release import InfrahubDropdownReleaseOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.infrahub.application import InfrahubApplication
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger("all.sregym.problems.infrahub")

DEVICE = "NetworkDevice"
#: NetworkDevice Dropdown attributes and their choices before the incident.
ORIGINAL = {"site": ["dc1", "dc2", "dc3"], "status": ["active", "planned", "decommissioned"]}
#: The two teams' changes, in send order: (attribute, choice, label).
CHANGES = [("site", "dc4", "DC4 Frankfurt"), ("status", "rma", "Returned for repair (RMA)")]
RELEASED = {"site": ["dc4"], "status": ["rma"]}
CHANGE_GAP_SECONDS = 0.3
MAX_ATTEMPTS = 4
#: The incident ages this long (inventory-sync keeps running) before the agent is handed the cluster.
PROPAGATION_S = 25 * 60

ROOT_CAUSE = (
    "Two teams changed the NetworkDevice schema 0.3 s apart with ordinary GraphQL SchemaDropdownAdd mutations on "
    "infrahub-server: the data-center team added the choice dc4 to the site Dropdown, the lifecycle team added the "
    "choice rma to the status Dropdown. Both returned ok: true and the schema briefly held dc4, but the stored "
    "NetworkDevice schema ended up with rma and without dc4, on every API worker. Upstream Infrahub 1.11.9 "
    "concurrency defect in the GraphQL schema mutations (graphql/mutations/schema.py): SchemaDropdownAdd copies "
    "the whole NetworkDevice node schema from the worker's in-memory registry before it takes any lock "
    "(db.schema.get(..., duplicate=True)), appends its choice to that copy, and passes the whole node to "
    "update_registry. Only update_registry takes global_schema_lock; inside it the copy replaces the kind "
    "wholesale and every attribute of the node is written back to Neo4j, with no check that the copy still "
    "matches what it overwrites. The two mutations were serialized by the lock, but the second (rma) wrote back a "
    "whole-node copy it had taken before the first (dc4) committed, so site's choice list reverted to dc1, dc2, "
    "dc3. Result: inventory-sync's registrations of dc4 devices fail with \"dc4 must be one of 'dc1, dc2, dc3' at "
    'site" while rma devices register. The stored schema lacks dc4, so restarting or reloading infrahub-server '
    "does not help. Correct repair: add dc4 to NetworkDevice.site again (SchemaDropdownAdd, or a schema load whose "
    "NetworkDevice carries the complete site and status choice lists), keeping rma, the existing choices and both "
    "attributes as Dropdowns, and keeping inventory-sync's dc4 and rma configuration. "
    "Grading requirements: D2 has a required core and a deeper detail. The core, required for D2-Q1, is that the "
    "second schema change wrote back a stale whole-node copy of the NetworkDevice schema (taken before the first "
    "change committed, that is before it held the schema lock), which reverted the first change's dc4. Accept "
    'observable equivalents, for example "the rma mutation overwrote the whole NetworkDevice schema node, or '
    'all of its attributes, with a copy read before the dc4 change landed", or a read-modify-write lost update of '
    "the whole node in which the read was not protected by the lock. The detail, which adds credit for D2-Q2 but is "
    "not required, is the code-level account: the mutation reads the kind from the in-memory registry with "
    "duplicate=True outside global_schema_lock and update_registry writes the whole node back (or, equivalently, "
    "that a second API worker's copy was also stale because its refresh was blocked while it waited for the lock). "
    "Answer No to D2-Q1, D2-Q2 and D2-Q3 for any of these wrong mechanisms the evidence invites: dc4 was never "
    "committed or the first mutation failed (it returned ok: true and was in the schema); someone or something "
    "removed dc4 afterwards (a SchemaDropdownRemove, a rollback, a migration or a later schema load); the two "
    "mutations ran unserialized or the schema lock was not taken or was lost (the lock serialized them; the copy "
    "was taken before it); a stale per-worker cache or registry that a restart or reload fixes (every worker "
    "serves the stored schema, which lacks dc4); or inventory-sync being misconfigured with an invalid site. A "
    "diagnosis that only says the two changes conflicted, without saying the second wrote back a stale copy of the "
    "whole node, lacks the core: answer No to D2-Q1."
)


class InfrahubConcurrentDropdownAddLostChoice(Problem):
    def __init__(self):
        app = InfrahubApplication()
        app.device_extra = ["status"]
        app.device_dropdowns = {a: list(c) for a, c in ORIGINAL.items()}
        app.seed_values = {"status": ["active", "active", "planned"]}
        app.inventory_fields["NetworkDevice"] = ["role", "site", "status"]
        app.inventory_values = {a: list(c[:2]) if a == "status" else list(c) for a, c in ORIGINAL.items()}
        super().__init__(app)
        self.kubectl = app.kubectl
        self.faulty_service = "infrahub-server"
        self.observed = {}
        self.release_started = False
        self.propagation_duration_s = PROPAGATION_S
        self.root_cause = self.build_structured_root_cause(
            component="infrahub-server", namespace=self.namespace, description=ROOT_CAUSE
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = InfrahubDropdownReleaseOracle(self, ORIGINAL, RELEASED)
        app.describe()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _outcome(results, views):
        """Both acknowledged; every worker serves one schema with rma and without dc4."""
        if not all(r and r.get("ok") for r in results) or len(views) != 1:
            return False
        served = json.loads(next(iter(views)))
        return (
            "dc4" not in served.get("site", [])
            and "rma" in served.get("status", [])
            and set(ORIGINAL["site"]) <= set(served.get("site", []))
            and set(ORIGINAL["status"]) <= set(served.get("status", []))
        )

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                # A clean slate: an in-place reset would leave extra schema changes in the logs.
                app.redeploy()
                self.mitigation_oracle.capture_baseline()
            self.release_started = True
            results = app.run_client("dropdown_release", changes=CHANGES, gap=CHANGE_GAP_SECONDS, exec_timeout=600)
            time.sleep(15)  # every worker processes the refresh broadcasts of both changes
            views = app.run_client("choice_views", samples=32, exec_timeout=600)
            record = {"changes": results, "views": views}
            self.observed[f"attempt_{attempt}"] = record
            logger.info("Dropdown change attempt %d: %s", attempt, record)
            if self._outcome(results, views):
                self.observed.update(attempts=attempt, changed_at=results[0]["started"])
                # The release goes live: inventory-sync starts recording dc4 and RMA devices.
                app.set_inventory_values("site", ORIGINAL["site"] + RELEASED["site"])
                app.set_inventory_values("status", ORIGINAL["status"][:2] + RELEASED["status"])
                self.mitigation_oracle.capture_handover()
                return
            self.release_started = False
        raise RuntimeError(f"Concurrent dropdown changes never lost a choice: {self.observed}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference mitigation: add the lost choice again with the same mutation."""
        if self.mitigation_oracle.evaluate().get("success"):
            return
        app = self.app
        for attribute, choice, label in CHANGES:
            result = app.run_client("dropdown_add", attribute=attribute, choice=choice, label=label)
            if not result["ok"] and "already exists" not in str(result.get("error")):
                raise RuntimeError(f"Re-adding {attribute}={choice} failed: {result}")
        app.set_inventory_values("site", ORIGINAL["site"] + RELEASED["site"])
        app.set_inventory_values("status", ORIGINAL["status"][:2] + RELEASED["status"])
