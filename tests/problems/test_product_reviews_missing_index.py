import csv
import io
import json
import shlex
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.product_reviews_missing_index_mitigation import (
    ProductReviewsMissingIndexMitigationOracle,
)
from sregym.conductor.problems.product_reviews_missing_index import (
    ProductReviewsMissingIndexAstronomyShop,
)

Problem = ProductReviewsMissingIndexAstronomyShop

BITMAP_PLAN = """Bitmap Heap Scan on productreviews  (cost=5.05..321.64 rows=98 width=91)
  Recheck Cond: ((product_id)::text = 'OLJCESPC7Z'::text)
  ->  Bitmap Index Scan on product_id_index  (cost=0.00..5.03 rows=98 width=0)"""
PARALLEL_SEQ_PLAN = """Gather  (cost=1000.00..7622.99 rows=104 width=91)
  Workers Planned: 2
  ->  Parallel Seq Scan on productreviews  (cost=0.00..6612.59 rows=43 width=91)"""


TABLE_RULES = {
    "id": "true|true|true",
    "product_id": "true|false|false",
    "username": "true|false|false",
    "description": "false|false|false",
    "score": "true|false|false",
    "(table)": "false|p|false",
}


@pytest.fixture
def problem():
    problem = Problem.__new__(Problem)
    problem.namespace = "astronomy-shop"
    problem._qualified = "reviews.productreviews"
    problem.index_column = "product_id"
    problem.index_name = "product_id_index"
    problem.kubectl = SimpleNamespace(exec_command=Mock(), exec_command_checked=Mock())
    problem.catalog_ids = ["OLJCESPC7Z", "66VCHSJNUP"]
    problem.probe_reviews = [("stargazer", "Great views, it's sharp.")]
    problem.probe_product_name = "National Park Foundation Explorascope"
    return problem


def explain_json(node_type, buffers, child=None):
    """What psycopg2 returns for EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)"""
    plan = {"Node Type": node_type, "Shared Hit Blocks": buffers - 1, "Shared Read Blocks": 1}
    if child:
        plan["Plans"] = [{"Node Type": child, "Shared Hit Blocks": buffers - 1, "Shared Read Blocks": 1}]
    return [{"Plan": plan, "Planning": {"Shared Hit Blocks": 3}}]


@pytest.mark.parametrize(
    "plan,expected",
    [
        (BITMAP_PLAN, True),
        ("Index Only Scan using reviews_pid_score on productreviews", True),
        ("Index Scan using productreviews_product_id_hash on productreviews", True),
        (PARALLEL_SEQ_PLAN, False),
        ("Seq Scan on productreviews", False),
        ("Aggregate\n  ->  Seq Scan on productreviews\n  SubPlan 1\n    ->  Index Scan using x on other", False),
        ("", False),
    ],
)
def test_plan_check(plan, expected):
    assert Problem.plan_uses_index_scan(plan) is expected


def test_history_is_deterministic_and_matches_the_measured_size():
    data, rows = Problem.history_csv([])
    assert (data, rows) == Problem.history_csv([])
    assert rows == 293_823
    parsed = list(csv.reader(io.StringIO(data)))
    assert len(parsed) == rows
    alphabet = set(Problem.PRODUCT_ID_ALPHABET)
    for product_id, username, description, score, _ in parsed[::997]:
        assert len(product_id) == 10 and set(product_id) <= alphabet
        assert username and description
        assert 1.0 <= float(score) <= 5.0
    assert len({row[0] for row in parsed}) == Problem.HISTORY_PRODUCTS


def test_history_does_not_look_cloned():
    parsed = list(csv.reader(io.StringIO(Problem.history_csv([])[0])))
    texts = {row[2] for row in parsed}
    assert len(texts) > 20_000
    assert len({row[1] for row in parsed}) > 10_000
    scores = [float(row[3]) for row in parsed]
    assert sum(score >= 4.0 for score in scores) / len(scores) > 0.7  # skewed high like the shop's own reviews
    bad_closers = set(Problem.CLOSERS["bad"])
    good_closers = {c for c in Problem.CLOSERS["good"] if c}
    for _, _, text, score, _ in parsed[::101]:
        if float(score) >= 4.0:
            assert not any(text.endswith(c) for c in bad_closers)
        if float(score) <= 2.0:
            assert not any(text.endswith(c) for c in good_closers)


def test_history_skips_products_still_in_the_catalog():
    taken = Problem.history_product_id(1)
    data, rows = Problem.history_csv([taken])
    assert taken not in {row[0] for row in csv.reader(io.StringIO(data))}
    assert rows < Problem.history_csv([])[1]


def test_history_reads_as_history_not_as_a_bulk_load():
    """Every review is dated in the three years before its product left the catalog"""
    data, count = Problem.discontinued_csv([])
    left = {row[0]: datetime.fromisoformat(row[2]) for row in csv.reader(io.StringIO(data))}
    assert count == len(left) == Problem.HISTORY_PRODUCTS
    assert min(left.values()).year == 2020 and max(left.values()).year == 2024
    reviews = list(csv.reader(io.StringIO(Problem.history_csv([])[0])))
    assert {row[0] for row in reviews} == set(left)
    for product_id, *_, written in reviews[::101]:
        age = left[product_id] - datetime.fromisoformat(written)
        assert timedelta(0) < age < timedelta(days=Problem.REVIEW_SPAN_DAYS + 1)
    assert len({row[4][:4] for row in reviews}) >= 7  # spread over years


def test_discontinued_products_skip_the_catalog_and_have_names():
    taken = Problem.history_product_id(1)
    rows = list(csv.reader(io.StringIO(Problem.discontinued_csv([taken])[0])))
    assert len(rows) == Problem.HISTORY_PRODUCTS - 1
    assert taken not in {row[0] for row in rows}
    assert all(name.split()[0] in Problem.PRODUCT_BRANDS for _, name, _ in rows)


def test_history_schema_dates_reviews_and_lists_discontinued_products(problem):
    problem._run_ddl = Mock()
    problem._prepare_history_schema()
    sql = problem._run_ddl.call_args.args[0]
    assert "ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT now()" in sql
    assert "CREATE TABLE IF NOT EXISTS catalog.discontinued_products" in sql  # init.sql may have made it
    assert "TRUNCATE catalog.discontinued_products" in sql  # a re-inject starts clean
    assert "COMMENT ON" not in sql  # nothing init.sql doesn't have


INIT_SQL = """-- Product Review Service: create tables
CREATE TABLE reviews.productreviews (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    product_id VARCHAR(16) NOT NULL,
    score NUMERIC(2,1) NOT NULL
);

CREATE INDEX product_id_index ON reviews.productreviews (product_id);

-- Product Catalog Service: create tables
CREATE TABLE catalog.products (
    id TEXT PRIMARY KEY,
    categories TEXT
);

-- Product Catalog Service: grant permission to schema
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO otelu;
"""


def test_init_sql_gains_the_history_schema_once():
    updated = Problem.init_sql_with_history(INIT_SQL)
    assert "    score NUMERIC(2,1) NOT NULL,\n    created_at TIMESTAMP NOT NULL DEFAULT now()\n);" in updated
    table = updated.index("CREATE TABLE catalog.discontinued_products (")
    # After catalog.products and before the schema-wide grant, which then covers it.
    assert updated.index("CREATE TABLE catalog.products (") < table < updated.index("GRANT SELECT ON ALL TABLES")
    assert Problem.init_sql_with_history(updated) == updated


@pytest.mark.parametrize("missing", ["CREATE TABLE reviews.productreviews (", "CREATE TABLE catalog.products ("])
def test_init_sql_fails_loudly_when_a_table_is_missing(missing):
    with pytest.raises(RuntimeError, match="no CREATE TABLE"):
        Problem.init_sql_with_history(INIT_SQL.replace(missing, "CREATE TABLE other ("))


def test_init_configmap_is_patched_once(problem):
    run = problem.kubectl.exec_command_checked
    run.return_value = json.dumps({"data": {"init.sql": INIT_SQL}})
    problem._patch_init_sql()
    get, patch = run.call_args_list
    assert "get configmap postgresql-init -n astronomy-shop" in get.args[0]
    assert "patch configmap postgresql-init -n astronomy-shop" in patch.args[0]
    assert json.loads(patch.kwargs["input_data"])["data"]["init.sql"] == Problem.init_sql_with_history(INIT_SQL)

    run.reset_mock()
    run.return_value = json.dumps({"data": {"init.sql": Problem.init_sql_with_history(INIT_SQL)}})
    problem._patch_init_sql()
    assert run.call_count == 1  # already there: nothing to patch


def test_init_configmap_without_init_sql_fails(problem):
    problem.kubectl.exec_command_checked.return_value = json.dumps({"data": {}})
    with pytest.raises(RuntimeError, match=r"no init\.sql"):
        problem._patch_init_sql()


def test_discontinued_seed_fails_loudly_when_copy_is_short(problem):
    problem._psql = Mock(return_value="COPY 12\n")
    with pytest.raises(RuntimeError, match="discontinued products"):
        problem._seed_discontinued_products()
    assert problem._psql.call_args.args[0].startswith("COPY catalog.discontinued_products (id, name, discontinued_on)")


def test_psql_uses_tcp_and_feeds_sql_on_stdin(problem):
    problem.kubectl.exec_command_checked.return_value = "1\n"
    sql = """SELECT 'it''s $HOME `id`' FROM reviews.productreviews;"""
    problem._psql(sql, tuples_only=True)
    call = problem.kubectl.exec_command_checked.call_args
    assert call.kwargs["input_data"] == sql
    script = shlex.split(call.args[0])[-1]
    assert "-h 127.0.0.1" in script
    assert "-tAf -" in script
    assert '-U "$POSTGRES_USER"' in script


def test_seed_fails_loudly_when_copy_is_short(problem):
    problem._max_review_id = Mock(return_value=50)
    problem._psql = Mock(return_value="COPY 12\n")
    with pytest.raises(RuntimeError, match="Seeding"):
        problem._seed_history()
    script = problem._psql.call_args.args[0]
    assert script.startswith(
        "COPY reviews.productreviews (product_id, username, description, score, created_at) FROM STDIN"
    )
    assert script.endswith("\\.\n")


def test_lookup_plans_splits_one_psql_call(problem):
    problem._psql = Mock(
        return_value="".join(f"---PLAN {i}\n{PARALLEL_SEQ_PLAN if i == 3 else BITMAP_PLAN}\n" for i in range(8))
    )
    plans = problem.lookup_plans()
    assert problem._psql.call_count == 1
    # 2 catalog products + 1 discontinued product + 1 with no reviews yet, 2 queries each
    assert len(plans) == 8
    assert any(Problem.history_product_id(1) in sql for sql in plans)
    assert any(Problem.history_product_id(Problem.HISTORY_PRODUCTS + 1) in sql for sql in plans)
    scanning = [sql for sql, plan in plans.items() if not Problem.plan_uses_index_scan(plan)]
    assert scanning == ["SELECT AVG(score) FROM reviews.productreviews WHERE product_id= '66VCHSJNUP'"]


def test_lookup_costs_runs_through_the_apps_own_connection(problem):
    statements = [q.format(product_id=pid) for pid in problem.plan_product_ids() for q in Problem.APP_QUERIES]
    plans = {sql: explain_json("Bitmap Heap Scan", 6, "Bitmap Index Scan") for sql in statements}
    plans[statements[1]] = explain_json("Aggregate", 5000, "Bitmap Heap Scan")
    plans[statements[4]] = explain_json("Gather", 5100, "Seq Scan")
    found = {"target": "otel|1761700000.5", "plans": plans}
    problem.kubectl.exec_command.return_value = "noise\n---PLANS " + json.dumps(found) + "\n"
    target, costs = problem.lookup_costs()
    assert target == "otel|1761700000.5"
    command = shlex.split(problem.kubectl.exec_command.call_args.args[0])
    assert "deploy/product-reviews" in command and "/venv/bin/python" in command
    script = command[-1]
    compile(script, "probe", "exec")
    assert "os.environ['DB_CONNECTION_STRING']" in script
    assert "options=" not in script  # must not override the app's own connection options
    assert "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)" in script
    assert "pg_postmaster_start_time()" in script
    assert [costs[sql]["buffers"] for sql in statements] == [6, 5000, 6, 6, 5100, 6, 6, 6]
    assert costs[statements[4]]["nodes"] == ["Gather", "Seq Scan"]


@pytest.mark.parametrize("output", ["", "psycopg2.OperationalError: connection refused", "---PLANS {not json"])
def test_lookup_costs_raises_when_it_cannot_read_plans(problem, output):
    problem.kubectl.exec_command.return_value = output
    with pytest.raises(RuntimeError):
        problem.lookup_costs()


def test_table_rules_reads_every_column(problem):
    problem._psql = Mock(return_value="id|true|true|true\nproduct_id|true|false|false\n")
    assert problem.table_rules() == {"id": "true|true|true", "product_id": "true|false|false"}


def test_table_rules_raises_on_sql_error(problem):
    problem._psql = Mock(return_value='ERROR:  relation "reviews.productreviews" does not exist')
    with pytest.raises(RuntimeError, match="does not exist"):
        problem.table_rules()


def test_recreate_index_replaces_whatever_is_there(problem):
    problem.product_id_indexes = Mock(return_value=["reviews.atk_brin"])
    problem._psql = Mock(return_value="")
    problem._run_ddl = Mock()
    problem._recreate_index()
    assert [call.args[0] for call in problem._run_ddl.call_args_list] == [
        "DROP INDEX reviews.atk_brin;",
        "CREATE INDEX product_id_index ON reviews.productreviews (product_id);",
    ]


def test_wait_for_reviews_ready_only_accepts_an_empty_table_when_told_to(problem):
    problem._READY_TIMEOUT_SECONDS = 0.01
    problem._READY_POLL_SECONDS = 0
    problem._psql = Mock(return_value="0\n")
    with pytest.raises(RuntimeError, match="queryable"):
        problem._wait_for_reviews_ready()
    problem._READY_TIMEOUT_SECONDS = 5
    problem._wait_for_reviews_ready(min_rows=0)


@pytest.fixture
def recovering(problem):
    problem.fault_injected = True
    problem._namespace_exists = Mock(return_value=True)
    problem._wait_for_reviews_ready = Mock()
    problem._recreate_index = Mock()
    problem._analyze = Mock()
    problem.heap_pages = Mock(return_value=5_594)
    problem.lookup_plans = Mock(return_value={"q1": BITMAP_PLAN})
    return problem


def test_recovery_raises_when_lookups_still_scan_a_large_table(recovering):
    recovering.lookup_plans.return_value = {"q1": PARALLEL_SEQ_PLAN}
    with pytest.raises(RuntimeError, match="still don't use it"):
        recovering.recover_fault()
    assert recovering.fault_injected is True


def test_recovery_accepts_a_seq_scan_on_a_tiny_table(recovering):
    recovering.heap_pages.return_value = 2
    recovering.lookup_plans.return_value = {"q1": "Seq Scan on productreviews"}
    assert recovering.recover_fault() is True
    assert recovering.fault_injected is False


def test_recovery_does_not_raise_when_the_agent_destroyed_the_table(recovering):
    recovering._wait_for_reviews_ready.side_effect = RuntimeError("did not become queryable")
    assert recovering.recover_fault() is False
    recovering._recreate_index.assert_not_called()


def test_lookup_plans_raises_on_sql_error(problem):
    problem._psql = Mock(return_value="ERROR:  relation does not exist")
    with pytest.raises(RuntimeError):
        problem.lookup_plans()


@pytest.mark.parametrize("response", ["", "ERROR: connection refused", "reviews-read-ok\nerror"])
def test_frontend_probe_fails_closed(problem, response):
    problem.kubectl.exec_command.return_value = response
    assert not problem.review_reads_work()


@pytest.mark.parametrize(
    "rows,ok",
    [
        ([{"username": "stargazer", "description": "Great views, it's sharp.", "score": "5.0"}], True),
        ([{"username": "other", "description": "unrelated", "score": "4.5"}], False),
        ([], False),
    ],
)
def test_frontend_probe_checks_the_original_reviews(problem, monkeypatch, rows, ok):
    product = {"id": "OLJCESPC7Z", "name": "National Park Foundation Explorascope"}

    def get(url, **kwargs):
        body = product if "/api/products/" in url else rows
        return SimpleNamespace(raise_for_status=Mock(), json=lambda: body)

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=get))

    def run_probe(command):
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                exec(shlex.split(command)[-1], {})
        except AssertionError:
            return "AssertionError"
        return out.getvalue()

    problem.kubectl.exec_command.side_effect = run_probe
    assert problem.review_reads_work() is ok
    product["name"] = "renamed"
    assert problem.review_reads_work() is False


@pytest.fixture
def healthy(problem, monkeypatch):
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: {"success": True})
    problem.history_rows, problem.history_checksum = 293_823, 42
    problem.history_fingerprint = Mock(return_value=(293_823, 42))
    problem.postgres_identity = {"uid": "a", "restarts": 0, "last_reason": None}
    problem.current_postgres_identity = Mock(return_value={"uid": "a", "restarts": 0, "last_reason": None})
    problem.original_reviews_intact = Mock(return_value=(True, "intact"))
    problem.postgres_pod_count = Mock(return_value=1)
    problem.missing_catalog_products = Mock(return_value=[])
    problem.table_rules_at_inject = dict(TABLE_RULES)
    problem.table_rules = Mock(return_value=dict(TABLE_RULES))
    problem.costs = {
        "q1": {"buffers": 6, "nodes": ["Bitmap Heap Scan", "Bitmap Index Scan"]},
        "q2": {"buffers": 9, "nodes": ["Aggregate", "Bitmap Heap Scan"]},
    }
    problem.lookup_costs = Mock(return_value=("otel|1761700000.5", problem.costs))
    problem.database_target = Mock(return_value="otel|1761700000.5")
    problem.heap_pages_at_inject = 5_000
    problem.review_reads_work = Mock(return_value=True)
    return problem


def verdict(problem):
    return ProductReviewsMissingIndexMitigationOracle(problem).evaluate()


def test_oracle_accepts_a_real_fix(healthy):
    assert verdict(healthy) == {"success": True}


def test_oracle_returns_the_base_failure_first(healthy, monkeypatch):
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: {"success": False, "reason": "base"})
    assert verdict(healthy)["reason"] == "base"
    healthy.history_fingerprint.assert_not_called()


def test_oracle_rejects_a_remaining_seq_scan(healthy):
    healthy.costs["q2"] = {"buffers": 5100, "nodes": ["Gather", "Seq Scan"]}
    result = verdict(healthy)
    assert result["reason"] == "fault_still_present"
    assert result["failure_class"] == FailureClass.AGENT_ERROR


@pytest.mark.parametrize("buffers,ok", [(250, True), (251, False), (1_130, False), (4_900, False)])
def test_oracle_judges_lookups_by_pages_read_not_plan_text(healthy, buffers, ok):
    """A BRIN index (1,130 pages live) or a btree with product_id second shows an index node but reads most pages"""
    healthy.costs["q2"] = {"buffers": buffers, "nodes": ["Bitmap Heap Scan", "Bitmap Index Scan"]}
    result = verdict(healthy)
    assert result.get("success") is ok
    if not ok:
        assert result["reason"] == "fault_still_present"


def test_oracle_accepts_a_cheap_seq_scan_on_a_small_partition(healthy):
    """Partitioning by product_id leaves a Seq Scan on one small partition. Tt reads few pages, so it's a fix"""
    healthy.costs["q2"] = {"buffers": 12, "nodes": ["Aggregate", "Seq Scan"]}
    assert verdict(healthy) == {"success": True}


def test_oracle_buffer_limit_comes_from_injection_and_has_a_floor(healthy):
    healthy.heap_pages_at_inject = 100
    healthy.costs["q2"]["buffers"] = 64
    assert verdict(healthy) == {"success": True}
    healthy.costs["q2"]["buffers"] = 65
    assert verdict(healthy)["reason"] == "fault_still_present"


@pytest.mark.parametrize(
    "now,reason,failure_class",
    [
        ({"uid": "a", "restarts": 0, "last_reason": None}, "review_history_deleted", FailureClass.AGENT_ERROR),
        (
            {"uid": "b", "restarts": 0, "last_reason": None},
            "review_history_lost_postgres_replaced",
            FailureClass.AGENT_ERROR,
        ),
        (
            {"uid": "a", "restarts": 1, "last_reason": "OOMKilled"},
            "review_history_lost_postgres_oom",
            FailureClass.AMBIGUOUS,
        ),
        (
            {"uid": "a", "restarts": 1, "last_reason": "Error"},
            "review_history_lost_postgres_restarted",
            FailureClass.AGENT_ERROR,
        ),
        (None, "review_history_lost_unattributed", FailureClass.AMBIGUOUS),
    ],
)
def test_oracle_attributes_lost_history(healthy, now, reason, failure_class):
    healthy.history_fingerprint.return_value = (0, 0)
    healthy.current_postgres_identity.return_value = now
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == (reason, failure_class)


@pytest.mark.parametrize("target", ["atk_copy|1761700000.5", "otel|1761799999.1"])
def test_oracle_rejects_an_app_repointed_at_another_database(healthy, target):
    """A trimmed copy (history deleted) in another database, or on another postgres, makes lookups cheap"""
    healthy.lookup_costs.return_value = (target, healthy.costs)
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == ("review_app_database_changed", FailureClass.AGENT_ERROR)


def test_oracle_rejects_changed_history(healthy):
    healthy.history_fingerprint.return_value = (293_823, 7)
    assert verdict(healthy)["reason"] == "review_history_modified"


def test_oracle_rejects_lost_original_reviews(healthy):
    healthy.original_reviews_intact.return_value = (False, "3 of 50 original reviews are gone")
    assert verdict(healthy)["reason"] == "original_reviews_not_preserved"


@pytest.mark.parametrize(
    "column,rule",
    [
        ("id", "true|true|false"),  # primary key dropped, or replaced by one that isn't on id alone
        ("id", "true|false|true"),  # identity lost
        ("product_id", "false|false|false"),  # NOT NULL lost
        ("score", None),  # column gone
        ("(table)", "true|p|false"),  # row-level security can hide the history from the app
        ("(table)", "false|u|false"),  # unlogged
        ("(table)", "false|p|true"),  # rows can sit in a child table, outside the primary key
    ],
)
def test_oracle_rejects_a_table_that_stopped_enforcing_its_rules(healthy, column, rule):
    healthy.table_rules.return_value = {**TABLE_RULES, column: rule}
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == ("review_table_rules_changed", FailureClass.AGENT_ERROR)
    assert column in result["detail"]["message"]


def test_oracle_rejects_a_second_postgres(healthy):
    healthy.postgres_pod_count.return_value = 2
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == ("postgres_scaled_out", FailureClass.AGENT_ERROR)


def test_postgres_pod_count_ignores_terminating_pods(problem):
    pods = [{"metadata": {"uid": "a"}}, {"metadata": {"uid": "b", "deletionTimestamp": "now"}}]
    problem.kubectl.exec_command.return_value = json.dumps({"items": pods})
    assert problem.postgres_pod_count() == 1


def test_postgres_pod_count_raises_when_it_cannot_list_pods(problem):
    problem.kubectl.exec_command.return_value = "Unable to connect to the server"
    with pytest.raises(RuntimeError, match="postgres pods"):
        problem.postgres_pod_count()


def test_oracle_rejects_lost_catalog_products(healthy):
    healthy.missing_catalog_products.return_value = ["66VCHSJNUP"]
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == ("catalog_products_not_preserved", FailureClass.AGENT_ERROR)


def test_missing_catalog_products_ignores_new_ones(problem):
    problem._catalog_ids = Mock(return_value=["OLJCESPC7Z", "NEWPRODUCT"])
    assert problem.missing_catalog_products() == ["66VCHSJNUP"]


def test_oracle_ignores_a_column_added_after_injection(healthy):
    healthy.table_rules.return_value = {**TABLE_RULES, "extra": "false|false|false"}
    assert verdict(healthy) == {"success": True}


@pytest.mark.parametrize(
    "check",
    [
        "postgres_pod_count",
        "history_fingerprint",
        "original_reviews_intact",
        "missing_catalog_products",
        "table_rules",
        "lookup_costs",
    ],
)
def test_oracle_blames_nobody_when_postgres_does_not_answer(healthy, check):
    getattr(healthy, check).side_effect = RuntimeError("ERROR: connection to server at 127.0.0.1 failed")
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == ("review_table_unreadable", FailureClass.AMBIGUOUS)


@pytest.mark.parametrize(
    "error",
    ['ERROR:  relation "reviews.productreviews" does not exist', 'ERROR:  schema "reviews" does not exist'],
)
def test_oracle_blames_the_agent_for_a_dropped_table(healthy, error):
    healthy.history_fingerprint.side_effect = RuntimeError(f"Could not fingerprint the review history: {error}")
    result = verdict(healthy)
    assert (result["reason"], result["failure_class"]) == ("review_table_missing", FailureClass.AGENT_ERROR)


def test_oracle_rejects_broken_frontend_reads(healthy):
    healthy.review_reads_work.return_value = False
    assert verdict(healthy)["reason"] == "review_frontend_read_failed"
