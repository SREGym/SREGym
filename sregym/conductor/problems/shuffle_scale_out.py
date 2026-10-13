"""Order routing and reservation consistency after a shop scale-out.

Large JSON revisions, retry state surviving rollback, and expiring payment keys
combine into a finance mismatch. The producer archive and Kafka retain original
commands; the provider exposes its ledger and a sandbox at /docs. Diagnosis uses
the multi-cause judge on main. Mitigation checks live service, history,
provider settlement, independent-SKU concurrency, replay and persistence.

Build from docker/apps/shuffle with ``docker build --target runtime -t
ghcr.io/sregym/shuffle-orders:20261013.1 .`` and ``docker build --target helper -t
ghcr.io/sregym/shuffle-helper:20261013.1 .``. Set SHUFFLE_IMAGE and
SHUFFLE_HELPER_IMAGE for other tags. Run ``python main.py --problem
shuffle_scale_out --agent codex --model gpt-6.1-sol --judge-model
gpt-6-astra --stages diagnosis mitigation``.
"""

import json
import os
import secrets
import shlex
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.utils.decorators import mark_fault_injected

IMAGE = os.getenv("SHUFFLE_IMAGE", "ghcr.io/sregym/shuffle-orders:20261013.1")
HELPER = os.getenv("SHUFFLE_HELPER_IMAGE", "ghcr.io/sregym/shuffle-helper:20261013.1")

SCHEMA = """CREATE ROLE shop_app LOGIN PASSWORD 'orders';
CREATE ROLE pool_svc LOGIN PASSWORD 'orders';
ALTER ROLE shop_app SET default_transaction_isolation = 'serializable';
ALTER ROLE pool_svc SET default_transaction_isolation = 'serializable';
CREATE SCHEMA shop AUTHORIZATION shop_app;
CREATE TABLE shop.skus (id text PRIMARY KEY, capacity integer NOT NULL CHECK (capacity >= 0));
CREATE TABLE shop.orders (id text PRIMARY KEY, sku text NOT NULL, cents integer NOT NULL, state text NOT NULL, receipt text NOT NULL, last_version bigint NOT NULL DEFAULT 0);
CREATE SEQUENCE shop.revisions;
SELECT setval('shop.revisions', extract(epoch FROM clock_timestamp())::bigint << 32, false);
GRANT USAGE,SELECT ON SEQUENCE shop.revisions TO shop_app,pool_svc;
CREATE INDEX ON shop.orders (sku) WHERE state='reserved';
CREATE TABLE shop.refunds (order_id text PRIMARY KEY, cents integer NOT NULL CHECK (cents > 0));
CREATE TABLE shop.refund_outbox (order_id text PRIMARY KEY, cents integer NOT NULL,
    revision bigint NOT NULL, key text NOT NULL, attempts integer NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL DEFAULT now(), provider_id text);
CREATE TABLE shop.applied_events (id text PRIMARY KEY);
-- Cold command journal: ingress offsets retired after durable archival.
CREATE TABLE shop.command_archive (id text PRIMARY KEY, body text NOT NULL, signature text NOT NULL,
    partition integer NOT NULL, offset_value bigint NOT NULL, archived_at timestamptz NOT NULL DEFAULT now());
GRANT USAGE ON SCHEMA shop TO shop_app,pool_svc;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA shop TO shop_app,pool_svc;
"""
CONTRACT = """Orders reserve one unit and collect 100 cents. Cancellation releases the unit and refunds once.
Rejected orders are refunded once. Warehouse eligibility receipts bind decisions to individual
orders. Reconciliation first applies every genuine cancellation, including cancelled rejections. For
any oversubscribed SKU, keep the earliest committed reservations, ranked by their original
creation-command Version; cancel and refund later excess reservations once (state compensated). Use
original producer commands, joined by event ID, for those revisions. Already rejected or refunded
orders must never be readmitted or refunded twice. Independent SKUs must process concurrently under
normal load; bulk order traffic must also maintain at least 40 newly committed orders per second.
Bulk workloads must settle within 90 seconds, including during online topic expansion. Order intake
must remain available during a live repair; there is no maintenance window. Each accepted command
must apply within 30 seconds, and no 20-second interval with arriving commands may pass without an
applied commit. New commands can revisit orders and SKUs that historical reconciliation is
repairing. The ingress replay window is bounded; closed offset ranges are archived before
retirement. The finance-reconcile job reports settlement discrepancies. The external payment
provider documents its API at http://payment-provider:8080/docs. Refund dispatch uses the globally
unique original producer revision as the idempotency key refund:<revision>; local refund rows are
acknowledgements, not proof that a provider credit settled. Provider adjustments reverse duplicate
credits. Reconcile each order against the provider ledger; net totals alone are insufficient.
""".strip()


class OrderShop(AstronomyShop):
    def run(self, args, namespace=None, data=None):
        return self.kubectl.exec_command_checked(
            f"kubectl -n {namespace or self.namespace} {args}", input_data=data, timeout=240
        )

    def apply(self, kind, name, spec, namespace=None, **fields):
        obj = {
            "apiVersion": "apps/v1" if kind == "Deployment" else "v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace or self.namespace},
            **fields,
        }
        if spec is not None:
            obj["spec"] = spec
        self.run("apply -f -", namespace, data=json.dumps(obj))

    def deployment(self, name, image, command, env=None, namespace=None, mounts=None, volumes=None, secret=None):
        container = {
            "name": name,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "command": command,
            "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
            "resources": {
                "requests": {"cpu": "30m", "memory": "64Mi"},
                "limits": {"cpu": "1000m", "memory": "2Gi" if name == "warehouse" else "512Mi"},
            },
        }
        if name != "pgbouncer":
            container["readinessProbe"] = {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 2}
        if mounts:
            container["volumeMounts"] = mounts
        if secret:
            container["envFrom"] = [{"secretRef": {"name": secret}}]
        self.apply(
            "Deployment",
            name,
            {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [container],
                        "volumes": volumes or [],
                    },
                },
            },
            namespace,
        )
        self.apply(
            "Service",
            name,
            {
                "selector": {"app": name},
                "ports": [
                    {"port": 5432 if name == "pgbouncer" else 8080, "targetPort": 5432 if name == "pgbouncer" else 8080}
                ],
            },
            namespace,
        )

    def helper(self, *args):
        out = self.run("exec deploy/warehouse -- python /app/harness.py " + shlex.join(args), "khaos")
        return json.loads(out.strip().splitlines()[-1])

    def restart(self, name):
        labels = json.loads(self.run(f"get deploy/{name} -o json"))["spec"]["selector"]["matchLabels"]
        selector = shlex.quote(",".join(f"{key}={value}" for key, value in labels.items()))
        old_pods = self.run(f"get pods -l {selector} -o name").split()
        self.run(f"rollout restart deploy/{name} --field-manager=kubectl-client-side-apply")
        self.run(f"rollout status deploy/{name} --timeout=180s")
        if old_pods:
            self.run("wait --for=delete --timeout=120s " + shlex.join(old_pods))

    def deploy(self):
        super().deploy()
        self.install_orders()

    def cleanup(self):
        super().cleanup()
        self.kubectl.exec_command(
            "kubectl -n khaos delete deployment/warehouse service/warehouse service/payment-provider "
            "service/kafka configmap/order-fixture-code pvc/provider-data --ignore-not-found --wait=true --timeout=120s"
        )

    def persist(self, name, path, **pod_fields):
        self.apply(
            "PersistentVolumeClaim",
            name + "-data",
            {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
        )
        container = {
            "name": name,
            "volumeMounts": [{"name": "data", "mountPath": path}],
            "readinessProbe": {"tcpSocket": {"port": 9092 if name == "kafka" else 5432}, "periodSeconds": 2},
        }
        if name == "kafka":
            # This fixture has one combined broker/controller; avoid a readiness/service bootstrap cycle.
            container["env"] = [
                {"name": "KAFKA_CONTROLLER_QUORUM_VOTERS", "value": "1@localhost:9093"},
                {"name": "KAFKA_HEAP_OPTS", "value": "-Xms400M -Xmx400M"},
            ]
            container["resources"] = {"requests": {"memory": "600Mi"}, "limits": {"memory": "2Gi"}}
        patch = {
            "spec": {
                "strategy": {"type": "Recreate", "rollingUpdate": None},
                "template": {
                    "spec": {
                        **pod_fields,
                        "containers": [container],
                        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": name + "-data"}}],
                    }
                },
            }
        }
        self.run(f"patch deploy/{name} --type=strategic -p " + shlex.quote(json.dumps(patch)))
        self.run(f"rollout status deploy/{name} --timeout=180s")

    def install_orders(self):
        self.kubectl.create_namespace_if_not_exist("khaos")
        self.apply(
            "Service",
            "kafka",
            {"type": "ExternalName", "externalName": f"kafka.{self.namespace}.svc.cluster.local"},
            "khaos",
        )
        key = secrets.token_hex(32)
        self.apply("Secret", "order-runtime", None, stringData={"EVENT_KEY": key})
        self.persist("kafka", "/tmp/kafka-logs", securityContext={"fsGroup": 1000})
        self.run("set resources deploy/postgresql --limits=memory=512Mi --requests=memory=128Mi")
        self.persist("postgresql", "/var/lib/postgresql/data")
        root = f"postgresql://root:otel@postgresql.{self.namespace}:5432/otel?sslmode=disable"
        from pathlib import Path

        code = Path(__file__).resolve().parents[3] / "docker" / "apps" / "shuffle"
        self.apply(
            "ConfigMap",
            "order-fixture-code",
            None,
            "khaos",
            data={
                "harness.py": (code / "harness.py").read_text(),
                "provider.py": (code / "provider.py").read_text(),
                "schema.sql": SCHEMA,
            },
        )
        self.apply(
            "PersistentVolumeClaim",
            "provider-data",
            {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
            "khaos",
        )
        self.deployment(
            "warehouse",
            HELPER,
            ["python", "/app/harness.py", "serve"],
            {"DATABASE_URL": root, "APP_NAMESPACE": self.namespace, "EVENT_KEY": key},
            "khaos",
            [{"name": "data", "mountPath": "/data"}, {"name": "code", "mountPath": "/app", "readOnly": True}],
            [
                {"name": "data", "persistentVolumeClaim": {"claimName": "provider-data"}},
                {"name": "code", "configMap": {"name": "order-fixture-code"}},
            ],
        )
        self.run("rollout status deploy/warehouse --timeout=180s", "khaos")
        self.helper("schema")
        self.apply(
            "Service", "warehouse", {"type": "ExternalName", "externalName": "warehouse.khaos.svc.cluster.local"}
        )
        self.apply(
            "Service",
            "payment-provider",
            {"selector": {"app": "warehouse"}, "ports": [{"port": 8080, "targetPort": 8081}]},
            "khaos",
        )
        self.apply(
            "Service",
            "payment-provider",
            {"type": "ExternalName", "externalName": "payment-provider.khaos.svc.cluster.local"},
        )
        self.apply(
            "ConfigMap",
            "pgbouncer",
            None,
            data={
                "pgbouncer.ini": "[databases]\notel = host=postgresql port=5432 dbname=otel user=pool_svc password=orders\n"
                "[pgbouncer]\nlisten_addr=0.0.0.0\nlisten_port=5432\nauth_type=plain\nauth_file=/etc/pgbouncer/userlist.txt\n"
                "pool_mode=transaction\ndefault_pool_size=24\nmax_client_conn=200\nserver_lifetime=3600\n"
                "ignore_startup_parameters=extra_float_digits\nlog_connections=0\nlog_disconnections=0\n",
                "userlist.txt": '"shop_app" "orders"\n',
            },
        )
        self.deployment(
            "pgbouncer",
            "edoburu/pgbouncer:v1.24.1-p0",
            ["/usr/bin/pgbouncer", "/etc/pgbouncer/pgbouncer.ini"],
            mounts=[{"name": "config", "mountPath": "/etc/pgbouncer"}],
            volumes=[{"name": "config", "configMap": {"name": "pgbouncer"}}],
        )
        for name in ("order-api", "cancel-service", "event-gateway", "order-state"):
            self.deployment(
                name,
                IMAGE,
                [name],
                {
                    "KAFKA_ADDR": "kafka:9092",
                    "DATABASE_URL": "postgresql://shop_app:orders@postgresql:5432/otel?sslmode=disable",
                    "WAREHOUSE_URL": "http://warehouse:8080",
                },
                secret="order-runtime",
            )
            self.run(f"rollout status deploy/{name} --timeout=180s")
        self.deployment(
            "refund-worker",
            IMAGE,
            ["refund-worker"],
            {
                "DATABASE_URL": "postgresql://shop_app:orders@postgresql:5432/otel?sslmode=disable",
                "PAYMENT_URL": "http://payment-provider:8080",
            },
        )
        self.run("rollout status deploy/refund-worker --timeout=180s")
        self.apply("ConfigMap", "order-operations", None, data={"service-contract.txt": CONTRACT})
        finance = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "finance-reconcile"},
            "spec": {
                "schedule": "*/2 * * * *",
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 2,
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "restartPolicy": "Never",
                                "containers": [
                                    {
                                        "name": "finance",
                                        "image": "curlimages/curl:8.14.1",
                                        "args": ["-fsS", "http://warehouse:8080/summary"],
                                    }
                                ],
                            }
                        }
                    }
                },
            },
        }
        self.run("apply -f -", data=json.dumps(finance))
        result = self.helper("traffic", "baseline", "128")
        if not result["settled"] or not self.helper("check")["success"]:
            raise RuntimeError("Order baseline failed")

    def get_app_summary(self):
        return (
            super().get_app_summary()
            + "\nThe order reservation service also supports paid reservations, cancellation and finance reconciliation."
        )


class ShuffleDiagnosis(LLMAsAJudgeOracle):
    def evaluate(self, solution, duration=None):
        result = super().evaluate(solution, duration)
        try:
            self.problem.app.helper("probe-start")
        except Exception as exc:
            result["live_probe_error"] = str(exc)
        return result


class ShuffleMitigation(MitigationOracle):
    def evaluate(self):
        app, phases = self.problem.app, {}

        # The CI lifecycle calls this oracle again after recover_fault resets
        # the fixture. That clean baseline has no incident probe or history;
        # exercise it separately without changing the agent's incident grading.
        if getattr(self.problem, "_recovered_baseline", False):
            workload = app.helper("traffic", "baseline", "128")
            history = app.helper("check")
            provider = app.helper("payment-check", "--wait")
            phases = {"baseline_traffic": workload, "baseline_history": history, "baseline_provider": provider}
            return {
                "success": bool(workload["settled"] and history["success"] and provider["success"]),
                "phases": phases,
            }

        def grade(name, operation):
            try:
                phases[name] = operation()
            except Exception as exc:
                phases[name] = {"success": False, "error": str(exc)}
            return phases[name]

        grade("live_slo", lambda: app.helper("probe-stop"))

        def pause():
            result = app.helper("pause")
            return {"success": bool(result["settled"]), **result}

        grade("drain", pause)
        grade("history", lambda: app.helper("check"))

        def exercise(kind, size):
            workload = app.helper("traffic", kind, str(size))
            state = app.helper("check")
            return {"workload": workload, **state, "success": bool(state["success"] and workload["settled"])}

        def traffic():
            checks = {
                kind: exercise(kind, size)
                for kind, size in (("migration", 180), ("eligibility", 144), ("capacity", 1200))
            }
            workload = checks["capacity"]["workload"]
            return {
                "success": all(check["success"] for check in checks.values())
                and workload["rate"] >= 40
                and workload["max_independent_inflight"] >= 4,
                "checks": checks,
            }

        grade("traffic", traffic)

        def replay():
            app.helper("replay")
            return app.helper("check")

        grade("replay", replay)

        def persistence():
            for name in ("event-gateway", "order-state", "pgbouncer"):
                app.restart(name)
            workloads = [
                app.helper("traffic", kind, size) for kind, size in (("contention", "240"), ("cancellation", "180"))
            ]
            state = app.helper("check")
            return {
                "workloads": workloads,
                **state,
                "success": bool(state["success"] and all(w["settled"] for w in workloads)),
            }

        grade("persistence", persistence)
        grade("provider", lambda: app.helper("payment-check", "--wait"))
        return {"success": all(phase["success"] for phase in phases.values()), "phases": phases}


class ShuffleScaleOut(Problem):
    run_default_workload = False

    def __init__(self):
        super().__init__(OrderShop())
        self.kubectl = self.app.kubectl
        causes = [
            self.build_structured_root_cause(component=component, namespace=self.namespace, description=text)
            for component, text in [
                (
                    "event-gateway",
                    "Both order producers correctly allocate monotonically increasing causal revisions from the shared "
                    "database sequence. Revisions use an epoch/counter namespace and exceed float64's exact integer range. "
                    "The create producer emits legacy JSON; cancellation emits structured CloudEvents. event-gateway "
                    "translates both from order-commands into canonical order-events by decoding through map[string]any "
                    "and re-encoding. Default encoding/json interface decoding converts numbers to float64, silently "
                    "rounding distinct int64 revisions into the same or nearby serialized integer. The gateway re-signs "
                    "the changed body, so downstream authentication and typed decoding succeed. order-state correctly "
                    "compares business versions, but a genuinely newer cancellation now appears equal and is suppressed "
                    "while its applied-event ID commits, leaving a reservation or rejection instead of cancellation and "
                    "its required refund. Producer allocation and worker SQL are locally correct; the loss occurs at "
                    "the compatibility gateway. Compare the same event IDs across input/output topics to locate it. "
                    "Preserve numeric tokens losslessly with UseNumber/RawMessage or equivalent typed translation, "
                    "or use valid terminal cancellation semantics independent of that corrupted token. Drain both "
                    "pipeline stages and explicitly reconcile suppressed cancellations/refunds from original retained "
                    "commands. The same rounding collapses revision-derived provider keys across different orders; "
                    "refund-worker accepts the first order's cached result for the second, leaving a "
                    "provider credit missing despite local refund rows. Applied IDs make replay a no-op; routing, isolation and projection refresh changes "
                    "cannot restore lost precision or repair history.",
                ),
                (
                    "order-state",
                    "The order-state worker correctly binds its prepared stock query to the Serializable transaction "
                    "on the first attempt, and both database roles use Serializable. The database detects conflicting "
                    "admissions and aborts an attempt with 40001. However, the per-event admission object is allocated "
                    "outside the retry loop. Its derived reserved/rejected state survives rollback; the next attempt "
                    "skips the availability query because that state is already set, and inserts the reservation using "
                    "the failed attempt's capacity decision. That write transaction has no fresh stock predicate read, "
                    "so Serializable cannot validate the stale decision and it can commit an excess reservation. "
                    "The observed serialization failures are working safeguards followed by an incomplete application retry. "
                    "Raising isolation, rebinding the already transaction-owned statement, refreshing sessions or bypassing "
                    "PgBouncer does not repair this retry-lifetime error. Retry the complete admission decision, including "
                    "stock reads and derived state, or atomically revalidate it on every write. Read-only warehouse eligibility "
                    "receipts can be reused, but stock admission cannot. Drain work, preserve valid reservations and "
                    "compensate exactly the excess with one refund each; applied-event IDs make replay ineffective.",
                ),
                (
                    "refund-worker",
                    "The provider's brief 503 degradation sometimes committed a credit before returning the error. "
                    "Its documented idempotency keys retain the first result only for a finite window and cache "
                    "that 503 within the window. The refund worker treats every 503 as an unpaid attempt and "
                    "continues exponential retries after key expiry. At that point the same key is a new request, "
                    "so one owed refund can credit a customer twice while the local DB records one. The retry "
                    "policy would be harmless with permanent keys but is wrong for this provider's finite retention "
                    "and ambiguous responses. Provider documentation, the disruption history, retry timing and "
                    "per-order ledger establish the mechanism; reading the worker alone does not. Correct handling "
                    "must inspect the ledger before retrying an uncertain credit beyond retention or stop and "
                    "reconcile, then reverse each surplus credit exactly once. Aggregate totals can hide a duplicate "
                    "behind a missing credit caused by the separate gateway precision fault.",
                ),
            ]
        ]
        self.root_cause = "\n\n".join(causes)
        self.diagnosis_oracle = ShuffleDiagnosis(self, causes)
        self.mitigation_oracle = ShuffleMitigation(self)

    @mark_fault_injected
    def inject_fault(self):
        self.app.helper("scale")
        self.app.run(
            "set env deploy/order-state --field-manager=kubectl-client-side-apply "
            "DATABASE_URL=postgresql://shop_app:orders@pgbouncer:5432/otel?sslmode=disable"
        )
        self.app.run(
            'patch deploy/order-state --type=merge --field-manager=kubectl-client-side-apply -p \'{"spec":{"replicas":6}}\''
        )
        for name in ("order-state", "order-api", "cancel-service"):
            self.app.restart(name)
        time.sleep(8)
        if not self.app.helper("payment-check", "--wait")["success"]:
            raise RuntimeError("Baseline provider refunds did not settle")
        for _ in range(3):
            if _ == 0:
                self.app.helper("provider-fault")
            self.app.helper("traffic", "history", "64")
            if any(case["excess_after_cancellation"] for case in self.app.helper("check")["history_interactions"]):
                break
        else:
            raise RuntimeError("Archived history did not reproduce interacting repairs")
        self.app.helper("retire")
        if not self.app.helper("traffic", "migration", "180")["settled"]:
            raise RuntimeError("Partition expansion workload did not drain")
        for _ in range(3):
            self.app.helper("traffic", "history", "64")
            result = self.app.helper("check")
            if (
                result["overbooked_units"]
                and result["reasons"].get("cancellation")
                and any(case["excess_after_cancellation"] for case in result["history_interactions"])
            ):
                break
        else:
            raise RuntimeError("Scale-out did not reproduce both mechanisms with interacting history")
        self.app.helper("payment-wait")
        self.app.helper("freeze")
        self.app.run("create job --from=cronjob/finance-reconcile finance-initial")

    def recover_fault(self):
        # The lifecycle validator expects a healthy application after recovery.
        # Reset the incident to a fresh baseline; the reference repair remains
        # local qualification tooling, never part of the shipped image.
        self._recovered_baseline = False
        self.app.cleanup()
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        self.app.deploy()
        self._recovered_baseline = True
