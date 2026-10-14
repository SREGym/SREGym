"""The vendored Incident Arena contracts and the problems built from them."""

from pathlib import Path

import pytest
import yaml

from sregym.conductor.problem_sets import PROBLEM_SETS
from sregym.conductor.problems.contracts.incident_arena.task import TASKS_DIR, IncidentArenaTask
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.apps.components.incident_arena import LOAD_WINDOW_S, continuous_load_profile, deep_merge
from tests.incident_arena.cases import INCIDENT_ARENA_PROBLEM_CLASSES, INCIDENT_ARENA_PROBLEMS

ALL_TASKS = sorted(p.name for p in TASKS_DIR.iterdir() if p.is_dir())


# Incident Arena tasks whose faults no other ported task covers (see docs/incident-arena.md).
PORTED_TASKS = ["002", "005", "006", "007", "008", "009", "013", "014", "018", "019"]


@pytest.mark.parametrize("name", ["frappe", "Frappe", "saleor", "Saleor", "slack-spine", "slack_spine", "Slack Spine"])
def test_catalog_constructs_apps_with_their_own_assets(offline_cluster, name):
    registry = AppRegistry()
    app = registry.get_app_instance(name)
    assert app.config_file == registry.get_app_config_file(name)
    assert app.values_file.parent == app.metadata_file.parent
    assert app.values_file.is_file()
    assert Path(app.helm_configs["chart_path"]).is_dir()
    assert app.namespace == registry.get_app_metadata(name)["Namespace"]
    assert registry.get_definition(name).service_port == app.frontend_port


def test_each_ported_task_is_vendored_once():
    assert [t[:3] for t in ALL_TASKS] == PORTED_TASKS
    assert sorted(cls.TASK for cls in INCIDENT_ARENA_PROBLEM_CLASSES) == ALL_TASKS
    assert len(INCIDENT_ARENA_PROBLEMS) == len(PORTED_TASKS)


def test_ported_problems_are_ordinary_problems():
    # No separate suite and no source prefix: they are selected like any other problem.
    assert all(not pid.startswith("incident_arena") for pid in INCIDENT_ARENA_PROBLEMS)
    for problem_set in PROBLEM_SETS.values():
        assert not set(problem_set) & set(INCIDENT_ARENA_PROBLEMS)


def test_problem_ids_are_registered(monkeypatch):
    import sregym.conductor.problems.registry as registry_module

    monkeypatch.setattr(registry_module, "KubeCtl", lambda: None)
    registry = registry_module.ProblemRegistry()
    for problem_id, cls in INCIDENT_ARENA_PROBLEMS.items():
        assert registry.get_problem(problem_id) is cls


def test_ported_lite_suite_matches_current_lite_order():
    from sregym.conductor.problems.lite_ia import LITE_IA_PORTS

    expected = tuple(LITE_IA_PORTS[problem_id][0] for problem_id in PROBLEM_SETS["sregym-lite"])
    assert PROBLEM_SETS["sregym-lite-ia"] == expected


def test_all_fault_ports_remain_registered(monkeypatch):
    import sregym.conductor.problems.registry as registry_module
    from sregym.conductor.problems.ia_ports import IA_PORT_PROBLEMS
    from sregym.conductor.problems.lite_ia import LITE_IA_PROBLEMS

    monkeypatch.setattr(registry_module, "KubeCtl", lambda: None)
    registry = registry_module.ProblemRegistry()
    for problem_id, factory in {**LITE_IA_PROBLEMS, **IA_PORT_PROBLEMS}.items():
        assert registry.get_problem(problem_id) is factory


def test_frappe_enables_scheduler_after_startup(offline_cluster, monkeypatch):
    from sregym.service.apps.components.incident_arena import IncidentArenaApplication
    from sregym.service.apps.frappe.application import Frappe

    app = Frappe()
    calls = []
    monkeypatch.setattr(IncidentArenaApplication, "deploy", lambda self: calls.append("ready"))
    monkeypatch.setattr(app, "site_python", lambda body: calls.append(body))
    app.deploy()
    assert calls[0] == "ready"
    assert "enable_scheduler()" in calls[1] and "frappe.db.commit()" in calls[1]


@pytest.mark.parametrize("slug", ALL_TASKS)
def test_task_contract_is_complete(slug):
    task = IncidentArenaTask.load(slug)
    assert task.ticket and "declare_repair_complete" not in task.ticket
    assert "submit_incident_report" not in task.ticket
    assert task.answer_key and all({"service", "component"} <= set(f) for f in task.answer_key)
    thresholds = task.thresholds
    assert {"error_rate_max", "goodput_min_ratio", "p99_ms_by_phase"} <= set(thresholds)
    assert task.soak_s > 0
    name, profile = task.load_profile()
    assert name and isinstance(profile, dict)
    rendered = yaml.safe_load(continuous_load_profile(name, profile))["profiles"][name]
    assert rendered["loop"] is True and rendered["declare_deadline_s"] == LOAD_WINDOW_S
    assert all(ev.get("kind") != "admin_event" for ev in rendered.get("events", []))


def test_frappe_tasks_do_not_gate_latency_but_slack_and_saleor_do():
    assert not IncidentArenaTask.load(ALL_TASKS[0]).gates_latency
    assert IncidentArenaTask.load(ALL_TASKS[6]).gates_latency
    assert IncidentArenaTask.load(ALL_TASKS[8]).gates_latency


def test_continuous_profile_keeps_maintenance_epochs_but_drops_admin_events():
    profile = {
        "base": "maintenance_collision_temporal",
        "declare_deadline_s": 3690.0,
        "events": [
            {"kind": "maintenance_epoch", "event_id": "epoch", "fire_at_s": 0.0},
            {"kind": "admin_event", "event_name": "read_consistency_strict", "fire_at_s": 40.0},
        ],
    }
    rendered = yaml.safe_load(continuous_load_profile("p", profile))["profiles"]["p"]
    assert [ev["kind"] for ev in rendered["events"]] == ["maintenance_epoch"]
    assert profile["events"][1]["kind"] == "admin_event"  # the input is not mutated


def test_deep_merge_replaces_lists_and_merges_maps():
    merged = deep_merge({"a": {"b": 1, "l": [1]}, "c": 1}, {"a": {"d": 2, "l": [2]}})
    assert merged == {"a": {"b": 1, "d": 2, "l": [2]}, "c": 1}


@pytest.mark.parametrize("problem_cls", INCIDENT_ARENA_PROBLEM_CLASSES, ids=lambda c: c.PROBLEM_ID)
def test_problem_constructs_offline(offline_cluster, problem_cls):
    problem = problem_cls()
    assert problem.problem_id == problem_cls.PROBLEM_ID
    assert problem.legs, "every incident injects at least one fault leg"
    # Frappe and Slack carry an app-wide scope guard; Saleor's single leg grades its own scope.
    assert problem.guards or problem_cls.PROBLEM_ID.startswith("saleor_")
    # The agent sees the original ticket plus the app's ground rules.
    assert problem.task.ticket.splitlines()[0][:40] in problem.app.description
    assert "Ground rules" in problem.app.description
    # The diagnosis ground truth names every graded component.
    assert problem.root_cause.startswith("[fault_spec]")
    for finding in problem.task.answer_key:
        assert finding["component"] in problem.root_cause
    assert len(problem.root_causes) == len(problem.task.answer_key)
    expected = problem.root_causes[0] if len(problem.root_causes) == 1 else problem.root_causes
    assert problem.diagnosis_oracle.expected == expected
    # Load generator runs the task's profile, looped for the whole run.
    loadgen = problem.app.deploy_overrides["loadgen"]
    profiles = yaml.safe_load(loadgen["profilesYaml"])["profiles"]
    assert loadgen["profile"] in profiles
    assert problem.mitigation_oracle is not None and problem.diagnosis_oracle is not None
    assert problem.outcome_spec().soak_s == problem.task.soak_s


def test_image_tier_tasks_deploy_their_release_build(offline_cluster):
    from sregym.conductor.problems.apps.slack_spine.lock_leaks import SlackDistractorVolumeSeqLock, SlackSeqLockLeak

    for cls in (SlackSeqLockLeak, SlackDistractorVolumeSeqLock):
        problem = cls()
        assert problem.app.deploy_overrides["images"]["app"] == problem.task.task_values["images"]["app"]


def test_compliance_window_is_context_not_a_cause(offline_cluster):
    from sregym.conductor.problems.apps.slack_spine.strict_mode import SlackSendsFailComplianceWindow

    problem = SlackSendsFailComplianceWindow()
    assert "read_consistency_strict" not in problem.root_cause
    assert "channel.db-pool" in problem.root_cause


def test_event_legs_carry_their_latent_hold_dose(offline_cluster):
    from sregym.conductor.problems.apps.slack_spine.store_consistency import SlackLoginsUnreadSendsAllSlow

    roles = SlackLoginsUnreadSendsAllSlow().app.deploy_overrides["app"]["roles"]
    assert {r: roles[r]["env"]["STORE_HOLD_MS"] for r in roles} == {
        "auth": "250",
        "workspace": "250",
        "notification": "250",
    }


def test_only_the_image_fault_problems_skip_the_healthy_latency_baseline():
    unhealthy = {pid for pid, cls in INCIDENT_ARENA_PROBLEMS.items() if not cls.HEALTHY_BASELINE}
    assert unhealthy == {"slack_seq_lock_leak", "slack_distractor_volume_seq_lock"}


@pytest.mark.parametrize(
    "problem_id",
    [
        "frappe_desk_and_queue_outage",
        "frappe_writes_and_queue_oom",
        "slack_sends_fail_strict_mode",
    ],
)
def test_compound_diagnosis_requires_every_cause(offline_cluster, monkeypatch, problem_id):
    problem = INCIDENT_ARENA_PROBLEMS[problem_id]()
    oracle = problem.diagnosis_oracle

    def evaluate_cause(solution, expectation, display_expectation=None):
        assert all(cause in expectation for cause in problem.root_causes)
        passed = display_expectation in solution
        return {"success": passed, "accuracy": 100 if passed else 0}

    monkeypatch.setattr(oracle, "_evaluate_single", evaluate_cause)
    assert oracle.evaluate(problem.root_cause)["success"] is True
    for omitted in range(len(problem.root_causes)):
        partial = "\n\n".join(cause for index, cause in enumerate(problem.root_causes) if index != omitted)
        verdict = oracle.evaluate(partial)
        assert verdict["success"] is False
        assert verdict["accuracy"] == 0
        assert len(verdict["oracles"]) == len(problem.root_causes)


def test_diagnosis_mutations_stay_with_their_cause(offline_cluster):
    problem = INCIDENT_ARENA_PROBLEMS["frappe_desk_and_queue_outage"]()
    database, queue = problem.root_causes
    assert "SET GLOBAL max_user_connections = 8" in database
    assert "min-replicas-to-write" not in database
    assert "min-replicas-to-write" in queue
    assert "SET GLOBAL max_user_connections" not in queue
