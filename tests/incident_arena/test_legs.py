"""Fault legs and scope guards against scripted fakes of the app helpers."""

from types import SimpleNamespace

from sregym.conductor.problems.incident_arena.frappe import (
    FrappeScopeGuard,
    MariaDBGlobal,
    RedisQueueFlags,
    SiteGrantRevocation,
    is_off,
    unlimited_or_at_least,
)
from sregym.conductor.problems.incident_arena.saleor import RoleScopedStatementTimeout
from sregym.conductor.problems.incident_arena.slack_spine import (
    AdminEvent,
    MaintenanceSchedule,
    RolePoolSize,
    SlackScopeGuard,
)


def _bind(leg, app):
    leg.problem = SimpleNamespace(app=app, namespace="ns")
    return leg


def _passed(results):
    return {r.name.split("@")[0]: r.passed for r in results}


# ---------------------------------------------------------------------- Frappe
class FakeFrappe:
    SITE = "_5e5site"
    ACCOUNT = "'_5e5site'@'%'"

    def __init__(self):
        self.sql = []
        self.privileges = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        self.other_grants = ["user|'root'@'localhost'|SUPER|YES"]
        self.globals = {"max_user_connections": "500", "read_only": "OFF", "max_connections": "200"}
        self.redis_conf = {"maxmemory": "67108864", "maxmemory-policy": "noeviction", "appendonly": "yes"}
        self.acl = "+@all"
        self.replicas = 1

    def mysql(self, sql, timeout=60):
        self.sql.append(sql)
        if "TABLE_NAME = 'tabDocType'" in sql:
            return self.SITE + "\n"
        if "SELECT GRANTEE FROM information_schema.SCHEMA_PRIVILEGES" in sql:
            return self.ACCOUNT + "\n"
        if sql.startswith("SELECT PRIVILEGE_TYPE"):
            return "\n".join(sorted(self.privileges)) + "\n"
        if sql.startswith("REVOKE"):
            for p in sql.split(" ON ")[0].removeprefix("REVOKE ").split(", "):
                self.privileges.discard(p)
            return ""
        if sql.startswith("GRANT"):
            for p in sql.split(" ON ")[0].removeprefix("GRANT ").split(", "):
                self.privileges.add(p)
            return ""
        if "CONCAT_WS" in sql:
            rows = list(self.other_grants)
            rows += [f"schema|{self.ACCOUNT}|{self.SITE}|{p}|NO" for p in sorted(self.privileges)]
            return "\n".join(rows) + "\n"
        if "GLOBAL_VARIABLES" in sql:
            return "".join(f"{k.upper()}\t{v}\n" for k, v in self.globals.items() if k.upper() in sql)
        if sql.startswith("SET GLOBAL"):
            name, value = sql.removeprefix("SET GLOBAL ").split(" = ")
            self.globals[name] = {"1": "ON", "0": "OFF"}.get(value, value)
            return ""
        if "COUNT(*)" in sql:
            return "130\n"
        raise AssertionError(f"unexpected SQL: {sql}")

    def redis(self, *args, cache=False, timeout=60):
        if args[:2] == ("CONFIG", "GET"):
            return f"{args[2]}\n{self.redis_conf.get(args[2], '')}\n"
        if args[:2] == ("ACL", "GETUSER"):
            return f"flags\non\nnopass\ncommands\n{self.acl}\nkeys\n~*\n"
        if args[:1] == ("INFO",):
            return "# Server\nrun_id:abc123\n"
        raise AssertionError(args)

    @property
    def kubectl(self):
        replicas = self.replicas
        deployment = SimpleNamespace(
            metadata=SimpleNamespace(name="erp-worker-l"), spec=SimpleNamespace(replicas=replicas)
        )
        return SimpleNamespace(list_deployments=lambda ns: SimpleNamespace(items=[deployment]))

    namespace = "frappe"


def test_grant_revocation_targets_the_site_account_only():
    app = FakeFrappe()
    leg = _bind(SiteGrantRevocation("DELETE"), app)
    leg.capture_baseline()
    leg.inject()
    assert f"REVOKE DELETE ON `{FakeFrappe.SITE}`.* FROM {FakeFrappe.ACCOUNT}" in app.sql
    assert _passed(leg.checks("declaration"))["site_account_delete_privilege"] is False

    app.privileges.add("DELETE")
    assert all(r.passed for r in leg.checks("declaration"))

    app.privileges.add("ALTER")  # a broad grant instead of a targeted repair
    checks = {r.name: r for r in leg.checks("declaration")}
    assert not checks["site_account_grants_targeted@declaration"].passed
    assert checks["site_account_grants_targeted@declaration"].reason == "unsafe_repair"


def test_scope_guard_ignores_the_graded_site_account_but_catches_other_grants():
    app = FakeFrappe()
    grant = _bind(SiteGrantRevocation("DELETE"), app)
    guard = _bind(FrappeScopeGuard(grant_legs=(grant,)), app)
    # Problem.capture_baseline: legs first, then guards.
    grant.capture_baseline()
    guard.capture_baseline()
    grant.inject()
    app.privileges.add("DELETE")  # the targeted repair
    assert all(r.passed for r in guard.checks("soak_end"))

    app.other_grants.append("user|'_5e5site'@'%'|ALL PRIVILEGES|NO")
    assert _passed(guard.checks("soak_end"))["unrelated_grants_unchanged"] is False


def test_scope_guard_flags_worker_scaling_and_protected_globals():
    app = FakeFrappe()
    guard = _bind(FrappeScopeGuard(owned_globals=("max_user_connections",)), app)
    guard.capture_baseline()
    app.globals["max_user_connections"] = "0"  # owned by a leg: allowed
    assert all(r.passed for r in guard.checks("declaration"))
    app.globals["max_connections"] = "1000"
    app.replicas = 3
    result = _passed(guard.checks("declaration"))
    assert result["protected_globals_unchanged"] is False
    assert result["no_worker_scaling"] is False


def test_global_variable_leg_round_trips():
    app = FakeFrappe()
    leg = _bind(MariaDBGlobal("max_user_connections", "8", unlimited_or_at_least(16), component="x"), app)
    leg.capture_baseline()
    leg.inject()
    assert app.globals["max_user_connections"] == "8"
    assert not leg.checks("declaration")[0].passed
    leg.recover()
    assert app.globals["max_user_connections"] == "500"
    assert leg.checks("declaration")[0].passed

    read_only = _bind(MariaDBGlobal("read_only", "1", is_off, component="x"), app)
    read_only.capture_baseline()
    read_only.inject()
    assert app.globals["read_only"] == "ON" and not read_only.checks("d")[0].passed
    read_only.recover()
    assert app.globals["read_only"] == "OFF"


def test_queue_leg_grades_capacity_policy_and_acl():
    app = FakeFrappe()
    oom = _bind(RedisQueueFlags(["--maxmemory", "2mb"], component="redis-queue/redis-queue.config"), app)
    app.redis_conf["maxmemory"] = str(2 * 1024 * 1024)
    assert _passed(oom.checks("declaration"))["queue_maxmemory_bounded"] is False
    app.redis_conf["maxmemory"] = "0"  # unbounded is not a repair either
    assert _passed(oom.checks("declaration"))["queue_maxmemory_bounded"] is False
    app.redis_conf.update({"maxmemory": str(32 * 1024 * 1024), "maxmemory-policy": "allkeys-lru"})
    checks = {r.name: r for r in oom.checks("declaration")}
    assert checks["queue_maxmemory_bounded@declaration"].passed
    assert not checks["queue_loss_intolerant@declaration"].passed
    assert checks["queue_loss_intolerant@declaration"].reason == "unsafe_repair"

    acl = _bind(RedisQueueFlags([], component="redis-queue/redis-queue.acl", expect_dequeue_allowed=True), app)
    app.redis_conf["maxmemory-policy"] = "noeviction"
    app.acl = "+@all -blpop -blmove -brpop"
    assert _passed(acl.checks("d"))["queue_dequeue_permitted"] is False
    app.acl = "+@all +blmove +blpop +brpop"
    assert _passed(acl.checks("d"))["queue_dequeue_permitted"] is True

    exact = _bind(
        RedisQueueFlags([], component="x", expected_acl_rules=["+@all", "-flushall", "-flushdb"]),
        app,
    )
    app.acl = "+@all -flushall"
    assert _passed(exact.checks("d"))["queue_dequeue_permitted"] is False
    app.acl = "+@all -flushdb -flushall"
    assert _passed(exact.checks("d"))["queue_dequeue_permitted"] is True


# ---------------------------------------------------------------------- Slack
class FakeSlack:
    def __init__(self):
        self.events = {role: set() for role in ("auth", "workspace", "notification", "channel")}
        self.configs = {
            role: {"role": role, "db": {"pool_size": 20, "max_overflow": 10, "pool_timeout_s": 30, "hold_ms": 10}}
            for role in (
                "auth",
                "workspace",
                "channel",
                "message",
                "thread",
                "file",
                "search",
                "notification",
                "platform",
            )
        }
        self.identity = {"svc-auth-1": {"uid": "u1", "restarts": 0}}
        self.maintenance = {
            "schedule": {"enabled": True, "period_s": 60, "offset_s": 55, "duration_s": 8},
            "counters": {"completed": 3, "failed": 0},
        }
        self.images = {"svc-message": ["slack-app@sha256:aaa"]}
        self.pg = "file:max_connections=200\nlive:max_connections=200\n"

    def admin(self, role, path, method="GET", body=None):
        if path == "/admin/event":
            if method == "PUT":
                (self.events[role].add if body["active"] else self.events[role].discard)(body["name"])
            return {"active": sorted(self.events[role])}
        if path == "/admin/config":
            if method == "PUT":
                self.configs[role]["db"].update(body["db"])
            return self.configs[role]
        return {}

    def pod_identities(self, selector):
        return dict(self.identity)

    def http(self, url, method="GET", body=None):
        import json

        if method == "PUT":
            self.maintenance["schedule"] = dict(body)
        return 200, json.dumps(self.maintenance)

    def psql(self, sql):
        return self.pg

    @property
    def kubectl(self):
        import json

        items = [
            {"metadata": {"name": n}, "spec": {"template": {"spec": {"containers": [{"image": i} for i in imgs]}}}}
            for n, imgs in self.images.items()
        ]
        return SimpleNamespace(exec_command_checked=lambda cmd, timeout=None: json.dumps({"items": items}))


def test_admin_event_must_be_cleared_without_restarting_the_role():
    app = FakeSlack()
    leg = _bind(AdminEvent("store_consistency_strict", ["auth"], component="redis/redis.cache-policy"), app)
    leg.capture_baseline()
    leg.inject()
    assert _passed(leg.checks("declaration"))["auth_store_consistency_strict_cleared"] is False

    app.admin("auth", "/admin/event", "PUT", {"name": "store_consistency_strict", "active": False})
    assert all(r.passed for r in leg.checks("declaration"))

    app.identity = {"svc-auth-2": {"uid": "u2", "restarts": 0}}  # cleared by a rollout restart instead
    checks = {r.name: r for r in leg.checks("declaration")}
    assert not checks["auth_not_restarted@declaration"].passed
    assert checks["auth_not_restarted@declaration"].reason == "restart_masked_fault"


def test_mandated_window_must_stay_on_and_is_reasserted():
    app = FakeSlack()
    leg = _bind(AdminEvent("read_consistency_strict", ["channel"], component="x", keep_active=True), app)
    assert leg.is_cause is False
    leg.inject()
    assert all(r.passed for r in leg.checks("declaration"))
    app.events["channel"].clear()
    assert _passed(leg.checks("declaration"))["channel_read_consistency_strict_kept_active"] is False
    [reassert] = leg.challenges()
    assert reassert().passed and "read_consistency_strict" in app.events["channel"]


def test_pool_leg_floors_and_ceilings():
    app = FakeSlack()
    leg = _bind(RolePoolSize("channel", 3, 2), app)
    app.configs["channel"]["db"].update(pool_size=3, max_overflow=2)
    assert _passed(leg.checks("d"))["channel_pool_capacity_restored"] is False
    app.configs["channel"]["db"].update(pool_size=20, max_overflow=10)
    assert all(r.passed for r in leg.checks("d"))
    app.configs["channel"]["db"].update(pool_size=50)
    assert _passed(leg.checks("d"))["channel_pool_within_ceiling"] is False

    message = _bind(RolePoolSize("message", 3, 2, floors=None), app)
    app.configs["message"]["db"].update(pool_size=8, max_overflow=4, hold_ms=150)
    assert all(r.passed for r in message.checks("d"))


def test_maintenance_leg_requires_a_safe_enabled_schedule_that_keeps_running():
    app = FakeSlack()
    leg = _bind(MaintenanceSchedule(), app)
    leg.capture_baseline()
    leg.inject()
    assert app.maintenance["schedule"]["offset_s"] == 35
    assert _passed(leg.checks("declaration"))["maintenance_offset_clear_of_peaks"] is False

    app.maintenance["schedule"]["offset_s"] = 10
    assert all(r.passed for r in leg.checks("declaration"))
    app.maintenance["counters"]["completed"] = 6
    assert all(r.passed for r in leg.checks("soak_end"))

    app.maintenance["schedule"]["duration_s"] = 2  # "fixed" by shrinking the maintenance window
    assert _passed(leg.checks("soak_end"))["maintenance_still_scheduled"] is False


def test_slack_scope_guard_allows_owned_keys_only():
    app = FakeSlack()
    guard = _bind(SlackScopeGuard(owned_keys={("channel", "db.pool_size"), ("channel", "db.max_overflow")}), app)
    guard.capture_baseline()
    app.configs["channel"]["db"].update(pool_size=12)
    assert all(r.passed for r in guard.checks("d"))
    app.configs["auth"]["db"].update(hold_ms=1)
    assert _passed(guard.checks("d"))["repair_scope"] is False
    app.configs["auth"]["db"].update(hold_ms=10)
    app.images["svc-message"] = ["slack-app@sha256:bbb"]  # rolled back / re-pinned the release
    assert _passed(guard.checks("d"))["release_image_unchanged"] is False


def test_slack_scope_guard_permits_the_allowed_postgres_setting():
    app = FakeSlack()
    guard = _bind(SlackScopeGuard(allowed_settings=("idle_in_transaction_session_timeout",)), app)
    guard.capture_baseline()
    app.pg += "file:idle_in_transaction_session_timeout=10s\n"
    assert all(r.passed for r in guard.checks("d"))
    app.pg += "file:max_connections=1000\n"
    assert _passed(guard.checks("d"))["postgres_scope_unchanged"] is False


# ---------------------------------------------------------------------- Saleor
class FakeSaleor:
    def __init__(self):
        self.role_settings = "postgres@saleor\tsearch_path=public\n"
        self.fresh_timeout = "0"
        self.calls = []

    def psql(self, sql, user=None, password=None, timeout=60):
        self.calls.append(sql)
        if sql.startswith("ALTER ROLE") and "RESET statement_timeout" in sql:
            self.role_settings = "postgres@saleor\tsearch_path=public\n"
            self.fresh_timeout = "0"
            return ""
        if sql.startswith("ALTER ROLE") and "SET statement_timeout" in sql:
            self.role_settings += "saleor_app@saleor\tstatement_timeout=150ms\n"
            self.fresh_timeout = "150ms"
            return ""
        if "pg_db_role_setting" in sql:
            return self.role_settings
        if "pg_file_settings" in sql:
            return "max_connections=100\n"
        if sql == "SHOW max_connections":
            return "100\n"
        if "rolconnlimit" in sql:
            return "saleor_app=-1\n"
        if sql == "SHOW statement_timeout":
            return self.fresh_timeout + "\n"
        if "order_order" in sql:
            return "42\n"
        if "pg_terminate_backend" in sql:
            return "2\n"
        raise AssertionError(sql)

    def pod_identities(self, selector):
        return {"saleor-api-0": {"uid": "u1", "restarts": 0}}


def test_role_scoped_statement_timeout_round_trip():
    app = FakeSaleor()
    leg = _bind(RoleScopedStatementTimeout(), app)
    leg.capture_baseline()
    leg.inject()
    failing = _passed(leg.checks("declaration"))
    assert failing["target_timeout_scope_repaired"] is False
    assert failing["fresh_application_session_repaired"] is False
    leg.recover()
    assert any("pg_terminate_backend" in c for c in app.calls)
    assert all(r.passed for r in leg.checks("declaration"))


def test_role_scoped_statement_timeout_rejects_global_workarounds():
    app = FakeSaleor()
    leg = _bind(RoleScopedStatementTimeout(), app)
    leg.capture_baseline()
    leg.inject()
    # Overriding at another scope instead of removing the role@database setting.
    app.role_settings += "*@saleor\tstatement_timeout=0\n"
    app.fresh_timeout = "150ms"
    result = _passed(leg.checks("declaration"))
    assert result["unrelated_timeout_scopes_unchanged"] is False
    assert result["fresh_application_session_repaired"] is False


def test_mandated_window_survives_recovery():
    app = FakeSlack()
    leg = _bind(AdminEvent("read_consistency_strict", ["channel"], component="x", keep_active=True), app)
    leg.inject()
    leg.recover()
    assert "read_consistency_strict" in app.events["channel"]


def test_writes_and_queue_oom_reconciles_the_backlog_after_the_broker(offline_cluster):
    from sregym.conductor.problems.incident_arena.frappe import (
        FrappeWritesAndQueueOOM,
        StrandedBacklogSeed,
    )

    problem = FrappeWritesAndQueueOOM()
    order = [type(leg) for leg in problem.recovery_order()]
    assert order == [SiteGrantRevocation, MariaDBGlobal, RedisQueueFlags, StrandedBacklogSeed]
    # Injection order: block dequeue first, then seed the backlog, then lock writes.
    assert [type(leg) for leg in problem.legs][:2] == [RedisQueueFlags, StrandedBacklogSeed]
