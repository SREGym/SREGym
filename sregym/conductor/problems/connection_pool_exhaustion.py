"""
Problem: a database connection cap set too low starves a service's connection pool.

This problem models a classic database connection-exhaustion failure on the Hotel Reservation 
app. The geo service keeps a pool of connections to its MongoDB backend (`mongodb-geo`). The 
backend is started with an incoming-connection cap far smaller than the pool the service needs, 
so once the service opens more than a handful of connections the database refuses new ones with 
"too many open connections" and the service's queries start failing. Both the database pod and 
the service pod stay Running and Ready, because neither carries a health probe, so pod-level 
checks show nothing wrong.

This is the failure mode that motivated managed connection poolers such as AWS RDS Proxy and 
PgBouncer: a fleet of pooled clients outgrows a fixed server-side connection limit, and the 
limit refuses connections while every process stays up. Restarting the clients or the database 
does not help, because the cap is still in effect after the restart.
"""

import json
import time

from sregym.conductor.oracles.connection_pool_mitigation import (
    ConnectionPoolMitigationOracle,
    count_open_connections,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ConnectionPoolExhaustion(Problem):
    """Cap mongodb-geo incoming connections far below the geo service's pool size."""

    DB_DEPLOYMENT = "mongodb-geo"
    CONSUMER_DEPLOYMENT = "geo"
    MAX_CONNS_FLAG = "--maxConns"
    # 5 is the smallest value mongod accepts, and far below the driver pool the service
    # opens, so the cap is pathological while the database still starts.
    INJECTED_MAX_CONNS = 5
    # The probe opens this many connections; a healthy server accepts the whole burst,
    # a capped one refuses after a few.
    PROBE_ATTEMPTS = 30
    HEALTHY_MIN_CONNECTIONS = 25
    SNAPSHOT_PATH = "/tmp/connection_pool_exhaustion_mongodb_geo_args.json"


    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = self.DB_DEPLOYMENT

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()

        self.root_cause = self.build_structured_root_cause(
            component=f"Deployment/{self.DB_DEPLOYMENT} backing service/{self.CONSUMER_DEPLOYMENT}",
            namespace=self.namespace,
            description=(
                f"`Deployment/{self.DB_DEPLOYMENT}` is started with an incoming-connection cap "
                f"(`{self.MAX_CONNS_FLAG} {self.INJECTED_MAX_CONNS}`) far below the connection pool that the "
                f"`{self.CONSUMER_DEPLOYMENT}` service maintains against it. Once the service needs more than a "
                "few connections, the database refuses new ones with a 'too many open connections' error and the "
                "service's database operations fail. Both pods stay Running and Ready because neither has a health "
                "probe, so this is not a crash, an eviction, an OOM, or a node problem, and the database process "
                "itself is healthy. A complete diagnosis should identify that the database is rejecting connections "
                "because its configured connection limit is too small for its client, rather than blaming the "
                "service pod or the node. A valid mitigation raises or removes that connection cap on "
                f"`{self.DB_DEPLOYMENT}` (which requires the database to restart to take effect) so the service's "
                "pool can be served. Restarting the service or the database without changing the cap does not help, "
                "and deleting or scaling down the database removes it instead of restoring capacity."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = ConnectionPoolMitigationOracle(
            problem=self,
            db_deployment=self.DB_DEPLOYMENT,
            consumer_deployment=self.CONSUMER_DEPLOYMENT,
            probe_attempts=self.PROBE_ATTEMPTS,
            healthy_min_connections=self.HEALTHY_MIN_CONNECTIONS,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        original_args = self._container_args(self.DB_DEPLOYMENT)
        self._save_args_snapshot(original_args)

        capped_args = self._strip_max_conns(original_args) + [self.MAX_CONNS_FLAG, str(self.INJECTED_MAX_CONNS)]
        self._replace_container_args(self.DB_DEPLOYMENT, capped_args)
        self._wait_for_rollout(self.DB_DEPLOYMENT)
        self._wait_for_cap_active()
        print(f"Service: {self.DB_DEPLOYMENT} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        restored_args = self._load_args_snapshot()
        if restored_args is None:
            restored_args = self._strip_max_conns(self._container_args(self.DB_DEPLOYMENT))

        self._replace_container_args(self.DB_DEPLOYMENT, restored_args)
        self._wait_for_rollout(self.DB_DEPLOYMENT)
        print(f"Service: {self.DB_DEPLOYMENT} | Namespace: {self.namespace}\n")

    def _container_args(self, deployment: str) -> list:
        spec = self._get_deployment_json(deployment)
        return list(spec["spec"]["template"]["spec"]["containers"][0].get("args") or [])

    @staticmethod
    def _strip_max_conns(args: list) -> list:
        # Drop an existing --maxConns flag (either "--maxConns N" or "--maxConns=N") so the
        # transform is idempotent across repeated injections.
        cleaned = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg == "--maxConns":
                skip_next = True
                continue
            if arg.startswith("--maxConns="):
                continue
            cleaned.append(arg)
        return cleaned


    def _replace_container_args(self, deployment: str, args: list):
        patch = [{"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": args}]
        out = self.kubectl.exec_command(
            f"kubectl patch deployment {deployment} -n {self.namespace} --type=json -p '{json.dumps(patch)}'"
        )
        print(f"Set Deployment/{deployment} args to {args}: {out.strip()}")


    def _wait_for_cap_active(self, timeout: int = 120):
        """Confirm the database now refuses connections beyond the injected cap."""
        deadline = time.monotonic() + timeout
        last_opened = None

        while time.monotonic() < deadline:
            opened = count_open_connections(self.kubectl, self.namespace, self.DB_DEPLOYMENT, self.PROBE_ATTEMPTS)
            last_opened = opened
            if 0 < opened < self.HEALTHY_MIN_CONNECTIONS:
                print(f"Deployment/{self.DB_DEPLOYMENT} now refuses connections after {opened} (cap is active)")
                return
            time.sleep(5)

        raise RuntimeError(
            f"Deployment/{self.DB_DEPLOYMENT} did not show a low connection cap within {timeout}s. "
            f"Last probe opened {last_opened} connections."
        )

    def _wait_for_rollout(self, deployment: str, timeout: int = 240):
        output = self.kubectl.exec_command(
            f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout={timeout}s"
        )
        lowered = output.lower()
        if "error" in lowered or "timed out" in lowered:
            raise RuntimeError(f"Deployment/{deployment} rollout did not complete: {output}")


    def _save_args_snapshot(self, args: list):
        with open(self.SNAPSHOT_PATH, "w") as file:
            json.dump(args, file)
        print(f"Saved original Deployment/{self.DB_DEPLOYMENT} args snapshot: {args}")


    def _load_args_snapshot(self):
        try:
            with open(self.SNAPSHOT_PATH) as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return None


    def _get_deployment_json(self, deployment: str) -> dict:
        raw = self.kubectl.exec_command(
            f"kubectl get deployment {deployment} -n {self.namespace} -o json"
        ).strip()
        if not raw:
            raise RuntimeError(f"kubectl returned no output for Deployment/{deployment}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse Deployment/{deployment} JSON: {exc}; output={raw[:500]!r}") from exc
        