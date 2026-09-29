import shlex
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.integer_overflow_primary_key_mitigation import IntegerOverflowPrimaryKeyMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.integer_overflow_primary_key_astronomy_shop import IntegerOverflowPrimaryKeyAstronomyShop


@pytest.fixture
def problem():
    problem = IntegerOverflowPrimaryKeyAstronomyShop.__new__(IntegerOverflowPrimaryKeyAstronomyShop)
    problem.namespace = "astronomy-shop"
    problem.kubectl = SimpleNamespace(exec_command=Mock())
    problem._resolve_id_sequence = Mock(return_value="reviews.renamed_sequence")
    problem._id_column_type = Mock(return_value="bigint")
    return problem


@pytest.mark.parametrize(
    "increment,last,called,expected,collision_free",
    [
        (1, 10, True, 90, True),
        (1, 10, False, 91, False),
        (3, 11, False, 30, True),
        (-3, -11, False, 30, True),
        (-3, -11, True, 29, True),
        (-1, -10, False, 91, False),
        (1, 100, True, 0, True),
        (-1, -100, True, 0, True),
    ],
)
def test_sequence_counts_remaining_values(problem, increment, last, called, expected, collision_free):
    problem._psql_super = Mock(return_value=f"{increment}|100|-100|f|{last}|10|-10|{'t' if called else 'f'}")
    capacity = problem._id_sequence_capacity()
    assert capacity["headroom"] == expected
    assert capacity["collision_free"] is collision_free
    assert "is_called FROM reviews.renamed_sequence" in problem._psql_super.call_args.args[0]


def test_large_increment_has_only_three_values(problem):
    problem._psql_super = Mock(return_value="4000000000000000000|9223372036854775807|1|f|1000|51|1|f")
    assert problem._id_sequence_capacity()["headroom"] == 3


@pytest.mark.parametrize(
    "name",
    [
        'reviews."review-ids"',
        'reviews."Review Ids"',
        'reviews."review\'ids"',
        'reviews."review""ids"',
        'reviews."$HOME"',
    ],
)
def test_sequence_identifiers_survive_shell_quoting(problem, name):
    problem._resolve_id_sequence.return_value = name
    problem.kubectl.exec_command.return_value = "1|100|-100|f|10|10|-10|t"
    assert problem._id_sequence_capacity()["headroom"] == 90
    query = shlex.split(problem.kubectl.exec_command.call_args.args[0])[-1]
    assert f"FROM {name}" in query
    assert "s.seqrelid = pg_get_serial_sequence(" in query


@pytest.mark.parametrize("method", ["_run_sql", "_psql_super"])
def test_sql_query_is_one_unchanged_shell_argument(problem, method):
    problem.kubectl.exec_command_checked = Mock()
    query = """SELECT 'review''s $HOME `id`', "review-ids" FROM reviews.productreviews;"""
    getattr(problem, method)(query)
    mock = problem.kubectl.exec_command_checked if method == "_run_sql" else problem.kubectl.exec_command
    assert shlex.split(mock.call_args.args[0])[-1] == query


def test_write_probe_uses_application_connection(problem):
    problem.kubectl.exec_command.return_value = "review-write-ok\n"
    assert problem._review_write_status() == "ok"
    command = shlex.split(problem.kubectl.exec_command.call_args.args[0])
    assert "deploy/product-reviews" in command
    assert "/venv/bin/python" in command
    script = command[-1]
    compile(script, "probe", "exec")
    assert "os.environ['DB_CONNECTION_STRING']" in script
    assert "connect_timeout=10" in script
    assert "statement_timeout=10000" in script
    assert "connection.rollback()" in script


@pytest.mark.parametrize(
    "error,status",
    [
        ('nextval: reached maximum value of sequence "reviews_id"', "exhausted"),
        ('nextval: reached minimum value of sequence "reviews_id"', "exhausted"),
        ("permission denied for table productreviews", "denied"),
        ("duplicate key value violates unique constraint", "collision"),
        ("connection refused", "other"),
    ],
)
def test_application_write_errors(problem, error, status):
    problem.kubectl.exec_command.return_value = error
    assert problem._review_write_status() == status


@pytest.mark.parametrize("response", ["", "ERROR: permission denied", "500", "reviews-read-ok\nerror"])
def test_frontend_probe_fails_closed(problem, response):
    problem.kubectl.exec_command.return_value = response
    assert not problem._review_reads_work()


def test_frontend_probe_checks_response_content(problem):
    problem.kubectl.exec_command.return_value = "reviews-read-ok\n"
    assert problem._review_reads_work()
    command = shlex.split(problem.kubectl.exec_command.call_args.args[0])
    script = command[-1]
    compile(script, "probe", "exec")
    assert "r.raise_for_status()" in script
    assert problem.SENTINEL_DESCRIPTION in script


@pytest.mark.parametrize("rows", [[], {}, [{"username": "other", "description": "unrelated", "score": "4.5"}]])
def test_frontend_probe_rejects_empty_or_wrong_data(problem, monkeypatch, rows):
    response = SimpleNamespace(raise_for_status=Mock(), json=lambda: rows)
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=lambda *args, **kwargs: response))

    def run_probe(command):
        with pytest.raises(AssertionError):
            exec(shlex.split(command)[-1], {})
        return "AssertionError"

    problem.kubectl.exec_command.side_effect = run_probe
    assert not problem._review_reads_work()


def test_oracle_rejects_broken_reads_after_database_checks(problem, monkeypatch):
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: {"success": True})
    problem._original_reviews_intact = Mock(return_value=(True, "intact"))
    problem._review_sentinel_present = Mock(return_value=True)
    problem._id_uniqueness_enforced = Mock(return_value=True)
    problem._review_write_status = Mock(return_value="ok")
    problem._id_sequence_capacity = Mock(
        return_value={
            "headroom": 10**12,
            "collision_free": True,
            "cycle": False,
            "fits_column": True,
        }
    )
    problem._review_reads_work = Mock(return_value=False)
    oracle = IntegerOverflowPrimaryKeyMitigationOracle(problem)
    assert oracle.evaluate()["success"] is False
    problem._review_reads_work.return_value = True
    assert oracle.evaluate()["success"] is True
