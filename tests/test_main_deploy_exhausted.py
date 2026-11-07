"""main.py's exit status when a deploy exhausted every retry.

main.py exited 0 unconditionally, so a caller could not tell a clean run from
one that lost an attempt to a deploy that never came up. The marker row
driver_loop() writes for it (deploy_failed) was real and archived, but nothing
between main.py's own exit and a person reading the CSV ever noticed it.

Only _deploy_exhausted_problems() is tested directly. The exit wiring around
it in main() drives a live Conductor and an asyncio API server, which this
suite has no cluster to run against; the helper carries the decision.
"""

import importlib.util
import sys
from pathlib import Path


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_deploy_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


main_mod = _load_main_module()


def _results(*rows: dict, agent: str = "codex") -> list[dict]:
    """The shape driver_loop returns: one {agent: rows} entry."""
    return [{agent: list(rows)}]


def test_a_clean_result_set_has_no_exhausted_problem():
    results = _results({"problem_id": "a", "attempt": 1, "deploy_failed": ""})
    assert main_mod._deploy_exhausted_problems(results) == set()


def test_the_marker_driver_loop_writes_is_a_bool():
    results = _results({"problem_id": "a", "attempt": 1, "deploy_failed": True})
    assert main_mod._deploy_exhausted_problems(results) == {"a"}


def test_the_marker_is_also_read_as_a_string():
    """A row that passed through a CSV carries the marker as text."""
    results = _results({"problem_id": "a", "attempt": 1, "deploy_failed": "True"})
    assert main_mod._deploy_exhausted_problems(results) == {"a"}


def test_every_exhausted_problem_is_named_across_agents():
    results = [
        {"codex": [{"problem_id": "a", "attempt": 1, "deploy_failed": True}]},
        {
            "claude": [
                {"problem_id": "b", "attempt": 1, "deploy_failed": ""},
                {"problem_id": "c", "attempt": 2, "deploy_failed": True},
            ]
        },
    ]
    assert main_mod._deploy_exhausted_problems(results) == {"a", "c"}


def test_the_literal_string_false_is_not_mistaken_for_exhaustion():
    results = _results({"problem_id": "a", "attempt": 1, "deploy_failed": "False"})
    assert main_mod._deploy_exhausted_problems(results) == set()


def test_no_results_means_no_exhausted_problem():
    assert main_mod._deploy_exhausted_problems([]) == set()


def test_the_exit_status_is_distinct_from_a_campaign_abort():
    assert main_mod.EXIT_DEPLOY_EXHAUSTED not in (0, 1)
