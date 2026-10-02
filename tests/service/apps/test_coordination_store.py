"""The coordination incident's physics: a recovery floor that cannot be compressed.

These tests pin the three properties the family exists to provide — a long
horizon with regression on premature action, tooling that lies rather than being
absent, and cost that accumulates irreversibly — without needing a cluster.
"""

import importlib

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh module instance with its state on a temporary volume."""
    monkeypatch.setenv("CONTROL_PATH", str(tmp_path))
    monkeypatch.setenv("MEMBER_COUNT", "3")
    monkeypatch.setenv("STABILITY_SECONDS", "60")
    monkeypatch.setenv("WARMING_SECONDS", "120")
    monkeypatch.setenv("ADMISSION_STEP_SECONDS", "45")
    module = importlib.import_module("sregym.service.apps.incident_runtime.coordination_store")
    importlib.reload(module)
    return module


def fresh(store):
    """A collapsed cluster -- what the agent is handed after injection."""
    return store.collapsed(store.settled(1000.0), 1000.0)


def healthy(store):
    """A settled, serving cluster -- what the application deploys."""
    return store.settled(1000.0)


def test_the_incident_starts_with_the_leader_unable_to_hold(store):
    state = fresh(store)

    assert store.write_latency_ms(state) > store.LATENCY_BUDGET_MS
    assert not store.leader_healthy(state)
    assert store.serve_capacity(state, 0) == 0.0


def test_shedding_load_is_what_lets_the_leader_hold(store):
    """Reducing streaming load first is the whole lesson of the original.

    Shedding has to be sufficient on its own, not merely necessary: every later
    phase is gated on a stable leader, so if uncompacted debt could keep latency
    over budget then compaction would never unlock and the incident would be
    unsolvable rather than long.
    """
    state = fresh(store)
    assert not store.leader_healthy(state)

    state["watch_subscriptions"] = store.WATCH_BUDGET

    assert store.write_latency_ms(state) <= store.LATENCY_BUDGET_MS
    assert store.leader_healthy(state)
    # Compaction then removes the remaining floor.
    state["compacted"] = True
    assert store.write_latency_ms(state) < store.write_latency_ms({**state, "compacted": False})


def test_partial_shedding_still_has_to_clear_the_latency_budget(store):
    """The budget is the contract, and the truth endpoint publishes both numbers."""
    state = fresh(store)
    state["watch_subscriptions"] = store.WATCH_BUDGET * 2 - 1

    assert not store.leader_healthy(state)
    reported = store.truth(state, 0.0)
    assert reported["watch_budget"] == store.WATCH_BUDGET
    assert reported["latency_budget_ms"] == store.LATENCY_BUDGET_MS


def test_restarting_without_shedding_cannot_help(store):
    """A fresh state file is the pod restart: the degradation is in the data."""
    state = fresh(store)
    store.save(state)

    reloaded = store.load()

    assert not store.leader_healthy(reloaded)
    assert reloaded["watch_subscriptions"] == state["watch_subscriptions"]


def test_compacting_a_churning_leader_adds_debt_instead_of_removing_it(store, tmp_path):
    """The attempt is itself a write storm, so rushing is actively worse."""
    state = fresh(store)
    before = state["compaction_debt"]
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/compact"

    result = handler.operate(state, {}, 1000.0)

    assert result["_code"] == 409
    assert state["compaction_debt"] > before


def test_compaction_requires_the_leader_to_have_held_for_the_full_window(store):
    state = fresh(store)
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, leader_since=1000.0)
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/compact"

    assert handler.operate(state, {}, 1000.0 + store.STABILITY_SECONDS - 5)["_code"] == 409
    assert not state["compacted"]

    # A full window measured from the *attempt*, not from the original election.
    assert handler.operate(state, {}, 1000.0 + 2 * store.STABILITY_SECONDS)["compacted"] is True


def test_polling_compaction_to_check_readiness_never_becomes_ready(store):
    """Impatience is the expensive mistake, and this is where it bites.

    A mutating attempt is a write storm that restarts the very window it is
    waiting on, so an agent that probes `compact` in a loop can never finish.
    Checking readiness is free, but only through the read-only truth endpoint.
    """
    state = fresh(store)
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, leader_since=1000.0)
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/compact"

    now = 1000.0
    for _ in range(12):
        now += store.STABILITY_SECONDS / 2
        assert handler.operate(state, {}, now)["_code"] == 409

    assert not state["compacted"]
    assert store.stable_seconds(state, now) < store.STABILITY_SECONDS
    # Waiting the window out without touching it does work.
    assert handler.operate(state, {}, now + store.STABILITY_SECONDS + 1)["compacted"] is True


def test_the_phases_are_ordered_and_cannot_be_skipped(store):
    state = fresh(store)
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, leader_since=1000.0)
    now = 1000.0 + store.STABILITY_SECONDS + 1
    handler = store.Coordinator.__new__(store.Coordinator)

    handler.path = "/v1/operator/rebuild-scheduler"
    assert handler.operate(state, {}, now)["_code"] == 409

    handler.path = "/v1/operator/compact"
    handler.operate(state, {}, now)
    handler.path = "/v1/operator/rebuild-scheduler"
    assert handler.operate(state, {}, now)["scheduler_state_fresh"] is True


def test_stale_placement_data_keeps_capacity_near_zero(store):
    """Compaction alone does not restore service; the scheduler is still stale."""
    state = fresh(store)
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, compacted=True, leader_since=0.0)
    now = 1000.0
    store.reconcile(state, now)

    assert store.leader_healthy(state)
    assert store.serve_capacity(state, now) == pytest.approx(0.1)


def test_caches_warm_only_after_everything_else_and_take_real_time(store):
    state = fresh(store)
    state.update(
        watch_subscriptions=store.WATCH_BUDGET,
        compaction_debt=0,
        compacted=True,
        scheduler_state_fresh=True,
        leader_since=1000.0,
    )
    ready = 1000.0 + store.STABILITY_SECONDS + 1

    store.reconcile(state, ready)
    assert state["cache_warm_since"] == ready
    assert store.cache_warm_fraction(state, ready) == 0.0

    half = ready + store.WARMING_SECONDS / 2
    assert store.cache_warm_fraction(state, half) == pytest.approx(0.5, abs=0.01)
    assert store.cache_warm_fraction(state, ready + store.WARMING_SECONDS) == 1.0


def test_admitting_everything_while_cold_re_collapses_the_cluster(store):
    """The restart-storm lesson: rushing admission costs you the progress."""
    state = fresh(store)
    state.update(
        watch_subscriptions=store.WATCH_BUDGET,
        compaction_debt=0,
        compacted=True,
        scheduler_state_fresh=True,
        leader_since=1000.0,
    )
    ready = 1000.0 + store.STABILITY_SECONDS + 1
    store.reconcile(state, ready)
    state["admitted_fraction"] = 1.0

    store.reconcile(state, ready + 1)

    assert state["regressions"] == 1
    assert state["admitted_fraction"] == 0.0
    assert not state["scheduler_state_fresh"]
    assert state["cache_warm_since"] is None
    assert state["dropped_requests"] > 0
    assert "caches" in state["last_regression_reason"]


def test_a_cautious_first_admission_step_does_not_regress(store):
    state = fresh(store)
    state.update(
        watch_subscriptions=store.WATCH_BUDGET,
        compaction_debt=0,
        compacted=True,
        scheduler_state_fresh=True,
        leader_since=1000.0,
    )
    ready = 1000.0 + store.STABILITY_SECONDS + 1
    store.reconcile(state, ready)
    state["admitted_fraction"] = store.COLD_ADMISSION_LIMIT

    store.reconcile(state, ready + 1)

    assert state["regressions"] == 0
    assert state["admitted_fraction"] == store.COLD_ADMISSION_LIMIT


def test_admission_steps_must_be_held_before_increasing(store):
    state = fresh(store)
    state.update(admitted_fraction=0.25, admitted_since=1000.0)
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/admit"

    early = handler.operate(state, {"fraction": 0.5}, 1000.0 + store.ADMISSION_STEP_SECONDS - 5)
    assert early["_code"] == 409
    assert state["admitted_fraction"] == 0.25

    later = handler.operate(state, {"fraction": 0.5}, 1000.0 + store.ADMISSION_STEP_SECONDS + 1)
    assert later["admitted_fraction"] == 0.5


def test_reducing_admission_is_always_allowed(store):
    """Backing off during an incident must never be rate limited."""
    state = fresh(store)
    state.update(admitted_fraction=1.0, admitted_since=1000.0)
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/admit"

    assert handler.operate(state, {"fraction": 0.0}, 1000.0 + 1)["admitted_fraction"] == 0.0


def test_dropped_requests_only_ever_increase(store):
    """Cost accumulates: there is no action that buys the losses back."""
    state = fresh(store)
    state["admitted_fraction"] = 1.0
    seen = []
    for tick in range(5):
        store.reconcile(state, 1000.0 + tick)
        seen.append(state["dropped_requests"])

    assert seen == sorted(seen)
    assert seen[-1] > 0
    # Full recovery does not reset the meter.
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, compacted=True, scheduler_state_fresh=True)
    store.reconcile(state, 2000.0)
    assert state["dropped_requests"] >= seen[-1]


def test_force_reset_destroys_a_member_permanently_and_regresses(store):
    state = fresh(store)
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/force-reset"

    result = handler.operate(state, {"member": "coordinator-1"}, 1000.0)

    assert result["members_available"] == 2
    assert state["regressions"] == 1
    # Repeating it is idempotent, not doubly destructive.
    handler.operate(state, {"member": "coordinator-1"}, 1001.0)
    assert state["destroyed_members"] == ["coordinator-1"]


def test_destroying_a_majority_loses_quorum_irrecoverably(store):
    state = fresh(store)
    handler = store.Coordinator.__new__(store.Coordinator)
    handler.path = "/v1/operator/force-reset"

    handler.operate(state, {"member": "coordinator-1"}, 1000.0)
    assert not store.quorum_lost(state)
    handler.operate(state, {"member": "coordinator-2"}, 1001.0)

    assert store.quorum_lost(state)
    # No sequence of correct actions can bring capacity back.
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, compacted=True, scheduler_state_fresh=True)
    assert not store.leader_healthy(state)
    assert store.serve_capacity(state, 2000.0) == 0.0


def test_status_lies_while_the_leader_churns(store):
    """The tool is broken by serving a stale snapshot, not by being unavailable."""
    state = fresh(store)

    reported = store.public_status(state, 1000.0)

    assert reported["healthy"] is True
    assert reported["stale"] is True
    assert reported["watch_subscriptions"] == 12  # the pre-incident value
    # The truth is available, just not from /status.
    actual = store.truth(state, 1000.0)
    assert actual["leader_healthy"] is False
    assert actual["watch_subscriptions"] == 96


def test_status_tells_the_truth_once_the_cluster_is_actually_stable(store):
    state = fresh(store)
    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0, compacted=True, leader_since=1000.0)
    now = 1000.0 + store.STABILITY_SECONDS + 1
    store.reconcile(state, now)

    reported = store.public_status(state, now)

    assert reported["stale"] is False
    assert reported["watch_subscriptions"] == store.WATCH_BUDGET


def test_the_full_key_scan_is_the_amplification_source(store):
    """The obvious diagnostic makes the incident worse, so it must be resisted."""
    source = __import__("pathlib").Path(store.__file__).read_text()
    assert '"/keys"' in source
    assert 'compaction_debt"] += 2000' in source
    assert "time.sleep(60)" in source


def test_a_perfect_operator_run_reaches_full_service(store):
    """There is a path through, and it takes real time rather than being blocked."""
    state = fresh(store)
    handler = store.Coordinator.__new__(store.Coordinator)
    now = 1000.0

    handler.path = "/v1/operator/shed"
    handler.operate(state, {"watch_subscriptions": store.WATCH_BUDGET}, now)
    # Latency is still over budget until the debt goes, so the leader cannot
    # hold yet -- which is why compaction is gated behind stability.
    state["compaction_debt"] = 0
    store.reconcile(state, now)
    now += store.STABILITY_SECONDS + 1

    handler.path = "/v1/operator/compact"
    assert handler.operate(state, {}, now)["compacted"] is True
    handler.path = "/v1/operator/rebuild-scheduler"
    assert handler.operate(state, {}, now)["scheduler_state_fresh"] is True
    store.reconcile(state, now)

    # Caches need their own real time before full admission is safe.
    now += store.WARMING_SECONDS
    assert store.cache_warm_fraction(state, now) == 1.0

    handler.path = "/v1/operator/admit"
    for step in (0.25, 0.5, 0.75, 1.0):
        handler.operate(state, {"fraction": step}, now)
        now += store.ADMISSION_STEP_SECONDS + 1
        store.reconcile(state, now)

    assert state["regressions"] == 0
    assert state["admitted_fraction"] == 1.0
    assert store.serve_capacity(state, now) == 1.0


def test_the_minimum_recovery_path_is_long_by_construction(store):
    """A 900-second budget was the ceiling before; this floor is deliberate."""
    floor = store.STABILITY_SECONDS + store.WARMING_SECONDS + 4 * store.ADMISSION_STEP_SECONDS

    assert floor >= 360
    # And a single rushed admission costs the whole warming phase again.
    assert store.WARMING_SECONDS >= 120


def test_unset_timestamps_are_none_not_zero(store):
    """`0.0` meant both "unset" and "the epoch".

    A leader elected at a timestamp of zero read back as never elected, so the
    cluster re-elected forever and a flawless recovery scored three regressions.
    """
    state = fresh(store)

    assert state["leader_since"] is None
    assert state["cache_warm_since"] is None
    assert state["admitted_since"] is None

    state.update(watch_subscriptions=store.WATCH_BUDGET, compaction_debt=0)
    store.reconcile(state, 0.0)
    elections = state["leader_elections"]
    # A leader elected at t=0 must stay elected.
    store.reconcile(state, 1.0)
    store.reconcile(state, 2.0)
    assert state["leader_elections"] == elections
    assert store.stable_seconds(state, 2.0) == 2.0


def test_the_in_cluster_request_script_is_valid_python_for_every_call_shape():
    """A GET embedded `null` into generated Python and died with a NameError.

    `json.dumps(None)` is the JSON literal `null`, which is fine in a payload and
    a NameError once it lands in a script. Every call shape has to compile, and
    reads are the shape that was broken -- so the whole family's diagnosis path
    was dead on a live cluster while the unit tests passed.
    """
    from sregym.service.apps.coordination_cluster import CoordinationCluster

    shapes = [
        ("/v1/internal/truth", None),
        ("/status", None),
        ("/ledger", None),
        ("/v1/operator/compact", {}),
        ("/v1/operator/shed", {"watch_subscriptions": 48}),
        ("/v1/operator/admit", {"fraction": 0.25}),
        ("/v1/operator/force-reset", {"member": "coordinator-1"}),
    ]
    for path, payload in shapes:
        script = CoordinationCluster.request_script(path, payload)
        compile(script, f"<{path}>", "exec")
        assert "null" not in script.split("url =")[0], (path, script)
        assert path in script


def test_a_read_sends_no_body_and_a_write_sends_one():
    from sregym.service.apps.coordination_cluster import CoordinationCluster

    read = CoordinationCluster.request_script("/v1/internal/truth")
    write = CoordinationCluster.request_script("/v1/operator/admit", {"fraction": 1.0})

    assert "body = None" in read
    assert '"fraction": 1.0' in write or "'fraction': 1.0" in write


def test_an_awkward_payload_value_cannot_break_the_script():
    """Values are embedded with repr, so quotes and newlines stay inert."""
    from sregym.service.apps.coordination_cluster import CoordinationCluster

    script = CoordinationCluster.request_script("/v1/operator/force-reset", {"member": "a'b\"c\nd"})

    compile(script, "<awkward>", "exec")


def test_the_application_deploys_healthy_and_serving(store):
    """The harness requires the oracle to pass before injection.

    An application that deploys already broken fails that gate, so the problem
    could never be screened -- and a baseline that is already degraded cannot
    show the incident caused anything. Found by checking the gate before
    launching a screen, not by the earlier unit tests.
    """
    state = healthy(store)

    assert store.leader_healthy(state)
    assert store.write_latency_ms(state) <= store.LATENCY_BUDGET_MS
    assert store.stable_seconds(state, 1000.0) >= store.STABILITY_SECONDS
    assert store.cache_warm_fraction(state, 1000.0) == 1.0
    assert store.serve_capacity(state, 1000.0) == 1.0
    assert state["admitted_fraction"] == 1.0


def test_a_healthy_cluster_drops_nothing(store):
    state = healthy(store)
    store.reconcile(state, 1000.0)
    store.reconcile(state, 1005.0)

    assert state["dropped_requests"] == 0
    assert state["regressions"] == 0


def test_inducing_the_collapse_breaks_it_and_pulls_traffic(store):
    """Admission goes to zero, so loss accrues from choices rather than the clock."""
    state = store.collapsed(healthy(store), 1000.0)

    assert not store.leader_healthy(state)
    assert state["watch_subscriptions"] == store.COLLAPSE["watch_subscriptions"]
    assert not state["compacted"]
    assert not state["scheduler_state_fresh"]
    # Traffic pulled: an agent is charged for its own premature admissions only.
    assert state["admitted_fraction"] == 0.0
    store.reconcile(state, 1005.0)
    assert state["dropped_requests"] == 0


def test_a_slow_agent_is_not_charged_for_elapsed_time(store):
    """With admission at zero the loss meter must not tick on its own."""
    state = fresh(store)
    for tick in range(60):
        store.reconcile(state, 1000.0 + tick * 10)

    assert state["dropped_requests"] == 0


def test_no_guide_states_the_diagnosis_or_the_recovery_order():
    """Diagnosis is the task and is never disclosed. This is the rule.

    It was broken in all three families. On the cascade screen the agent's first
    action was `cat /control/README.txt` and its next thirty commands applied
    what that file said, which measures instruction-following. Each guide is now
    a service or evidence reference: what exists, what the API is, what the
    documented requirement is -- never what went wrong or what to do about it.
    """
    from sregym.service.apps.coordination_cluster import SERVICE_REFERENCE
    from sregym.service.apps.gitlab_failover import FAILOVER_GUIDE
    from sregym.service.apps.mattermost_cascade import CASCADE_GUIDE

    forbidden = [
        # Causal explanation.
        "blocked rather than busy",
        "amplification source",
        "does not help",
        "last successful leader",
        "restarts that window",
        "staircase",
        # The recovery contract or ordering.
        "Recovery order",
        "Requirements for recovery",
        "keep the identity they already have",
        "necessarily",
        "is not recovery",
        "judged on that outcome",
        # Telling the agent which signal to distrust.
        "before trusting it",
        "Both are stale",
        "no series at all",
    ]
    for name, guide in (
        ("cascade", CASCADE_GUIDE),
        ("failover", FAILOVER_GUIDE),
        ("coordination", SERVICE_REFERENCE),
    ):
        flat = " ".join(guide.split())
        for phrase in forbidden:
            assert phrase not in flat, f"{name} guide still states: {phrase}"


def test_the_guides_still_make_each_task_solvable():
    """Withholding diagnosis must not withhold the tools.

    A custom operator API is not discoverable by any amount of kubectl, and a
    documented SLO is something on-call genuinely has. Removing those would make
    the tasks unsolvable rather than hard.
    """
    from sregym.service.apps.coordination_cluster import SERVICE_REFERENCE
    from sregym.service.apps.gitlab_failover import FAILOVER_GUIDE
    from sregym.service.apps.mattermost_cascade import CASCADE_GUIDE

    # The operator verbs exist nowhere else.
    for verb in ("shed", "compact", "rebuild-scheduler", "admit", "force-reset"):
        assert f"/v1/operator/{verb}" in SERVICE_REFERENCE, verb
    assert "DESTRUCTIVE" in SERVICE_REFERENCE

    # The capacity floor is a documented requirement, not a diagnosis.
    assert "capacity floor" in CASCADE_GUIDE
    assert "chat-gateway" in CASCADE_GUIDE

    # The evidence inventory says which artifacts exist, not which to use.
    assert "pre-partition.dump" in FAILOVER_GUIDE
    assert "last-replicated.dump" in FAILOVER_GUIDE
    assert "acceptance journal" in FAILOVER_GUIDE


def test_the_gates_stay_discoverable_from_the_api_alone():
    """Withholding the diagnosis must not make the task unsolvable.

    The refusal messages carry the gate semantics, so an agent can learn the
    rules by trying -- and pays a window for doing so, which is the task.
    """
    import importlib

    store = importlib.import_module("sregym.service.apps.incident_runtime.coordination_store")
    state = store.collapsed(store.settled(1000.0), 1000.0)
    handler = store.Coordinator.__new__(store.Coordinator)

    # While the leader is churning, the refusal says debt was added -- enough to
    # learn that the attempt was counterproductive.
    handler.path = "/v1/operator/compact"
    assert "added debt" in handler.operate(state, {}, 1000.0)["error"]

    # Once load is shed the leader holds, and the refusal then names the window
    # and says the attempt restarted it. That is how the gate is discoverable.
    handler.path = "/v1/operator/shed"
    handler.operate(state, {"watch_subscriptions": store.WATCH_BUDGET}, 1000.0)
    state["compaction_debt"] = 0
    store.reconcile(state, 1000.0)
    handler.path = "/v1/operator/compact"
    refusal = handler.operate(state, {}, 1010.0)["error"]
    assert "stability window" in refusal and "required" in refusal

    handler.path = "/v1/operator/rebuild-scheduler"
    assert "compact" in handler.operate(state, {}, 1010.0)["error"]

    # And the truth endpoint publishes every threshold the gates use.
    reported = store.truth(state, 1000.0)
    for key in ("latency_budget_ms", "watch_budget", "stability_required_seconds", "cold_admission_limit"):
        assert key in reported, key


def test_no_injected_evidence_states_the_diagnosis():
    """Stripping the guides was not enough: injection writes evidence too.

    A re-screen trace showed the agent reading `/control/incident-notes.txt`,
    which is written by `inject_fault` rather than by the guide. It had been
    telling the agent that CPU was low and falling and that a manual scale-up
    only held for a minute -- the whole task, in a second channel. The failover
    chat went further and instructed the responder not to restore the snapshot,
    which is the central decision.

    Evidence may report symptoms, actions taken and wrong hypotheses. It may not
    state a cause, name the misleading signal, or give the recovery contract.
    """
    import ast
    import pathlib

    forbidden = [
        "CPU LOW",
        "workers busy",
        "requests shed",
        "helped for under a minute",
        "use the gateway's own metrics",
        "do NOT restore",
        "we have taken writes since",
        "both regions acknowledged",
        "Neither set may be lost",
        "is not recovery",
        "were not discarded by this tool",
        "now resolve to different issues",
    ]
    problems = pathlib.Path("sregym/conductor/problems")
    for name in ("mattermost_capacity_cascade.py", "gitlab_regional_failover.py", "coordination_collapse.py"):
        source = (problems / name).read_text()
        # Every string literal in the problem, which covers whatever it writes
        # as evidence regardless of the helper used.
        literals = [
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        blob = " ".join(" ".join(x.split()) for x in literals)
        for phrase in forbidden:
            assert phrase not in blob, f"{name} evidence still states: {phrase}"


def test_the_injected_evidence_still_carries_a_wrong_hypothesis():
    """Real incident chatter contains bad guesses; that is part of the task."""
    import pathlib

    cascade = pathlib.Path("sregym/conductor/problems/mattermost_capacity_cascade.py").read_text()
    failover = pathlib.Path("sregym/conductor/problems/gitlab_regional_failover.py").read_text()

    assert "database is struggling" in cascade
    assert "partial restore" in failover
