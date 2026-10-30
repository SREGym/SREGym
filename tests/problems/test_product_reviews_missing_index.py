import csv
import io
import json
import shlex
import sys
from contextlib import redirect_stdout
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


@pytest.fixture
def problem():
    problem = Problem.__new__(Problem)
    problem.namespace = "astronomy-shop"
    problem._qualified = "reviews.productreviews"
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
    for product_id, username, description, score in parsed[::997]:
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
    for _, _, text, score in parsed[::101]:
        if float(score) >= 4.0:
            assert not any(text.endswith(c) for c in bad_closers)
        if float(score) <= 2.0:
            assert not any(text.endswith(c) for c in good_closers)


def test_history_skips_products_still_in_the_catalog():
    taken = Problem.history_product_id(1)
    data, rows = Problem.history_csv([taken])
    assert taken not in {row[0] for row in csv.reader(io.StringIO(data))}
    assert rows < Problem.history_csv([])[1]


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
    assert script.startswith("COPY reviews.productreviews (product_id, username, description, score) FROM STDIN")
    assert script.endswith("\\.\n")


def test_lookup_plans_splits_one_psql_call(problem):
    problem._psql = Mock(
        return_value="".join(f"---PLAN {i}\n{PARALLEL_SEQ_PLAN if i == 3 else BITMAP_PLAN}\n" for i in range(6))
    )
    plans = problem.lookup_plans()
    assert problem._psql.call_count == 1
    # 2 catalog products + 1 discontinued product, 2 queries each
    assert len(plans) == 6
    assert any(Problem.history_product_id(1) in sql for sql in plans)
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
    assert [costs[sql]["buffers"] for sql in statements] == [6, 5000, 6, 6, 5100, 6]
    assert costs[statements[4]]["nodes"] == ["Gather", "Seq Scan"]


@pytest.mark.parametrize("output", ["", "psycopg2.OperationalError: connection refused", "---PLANS {not json"])
def test_lookup_costs_raises_when_it_cannot_read_plans(problem, output):
    problem.kubectl.exec_command.return_value = output
    with pytest.raises(RuntimeError):
        problem.lookup_costs()


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


@pytest.mark.parametrize("check", ["history_fingerprint", "original_reviews_intact", "lookup_costs"])
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
