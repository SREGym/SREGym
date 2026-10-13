"""Evidence kinds per origin candidate (the generic part of jev_diag's triage)."""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace

from clients.jev_agent import evidence
from clients.jev_agent.notebook import Notebook
from clients.jev_agent.types import AgentContext, ToolResult

COMPS = {
    "deployment/orders": {"kind": "Deployment", "name": "orders", "namespace": "ns"},
    "deployment/stream": {"kind": "Deployment", "name": "stream", "namespace": "ns"},
    "deployment/api": {"kind": "Deployment", "name": "api", "namespace": "ns"},
    "deployment/db": {"kind": "Deployment", "name": "db", "namespace": "ns"},
}


def setup():
    ctx = AgentContext(app_info={}, namespaces=["ns"], snapshot=SimpleNamespace(components=COMPS), overview={})
    nb = Notebook({}, {})
    return ctx, nb


def add(nb, step, tool, target, lines, abnormal=0.9):
    nb.add(step, tool, target, ToolResult(lines=lines)).abnormal = abnormal


def test_kinds_for_workloads_objects_data_and_interactions():
    ctx, nb = setup()
    add(
        nb,
        1,
        "logs",
        "deployment/api",
        ["error lines name other components: deployment/db", "ongoing x9: call db timeout"],
    )
    add(nb, 2, "logs", "deployment/db", ["ongoing x4: FATAL: password authentication failed for user app"])
    add(nb, 3, "config_and_changes", "deployment/db", ["revision 2 change: container db env DB_PASSWORD added: x"])
    add(nb, 4, "describe", "deployment/api", ["pod api-1: Running; api ready=False restarts=3"])
    add(
        nb,
        5,
        "backend_probe",
        "deployment/stream",
        [
            "group g topic t partition 0: committed offset 20 unchanged over 16s while log end moved 90->98; consumer deployment/orders",
            'records from topic t partition 0 starting at the stalled offset 20 (read-only, no consumer group): Offset:20 {"id":',
        ],
    )
    add(
        nb,
        6,
        "config_and_changes",
        "deployment/orders",
        ["ConfigMap scripts (created 80s before symptom onset; not written since): x"],
    )
    add(nb, 7, "logs", "deployment/orders", ["ongoing x4: ERROR partition remains paused at offset=20"])
    kinds, facts = evidence.candidate_evidence(ctx, nb, "deployment/db")
    assert kinds == ["change", "pointed_at", "own_failure_logs"] and facts[0].startswith("change: revision 2")
    kinds, _ = evidence.candidate_evidence(ctx, nb, "deployment/api")
    assert kinds == ["symptoms", "error_logs_only"]
    assert evidence.candidate_evidence(ctx, nb, "deployment/orders")[0] == ["error_logs_only"]
    kinds, facts = evidence.candidate_evidence(ctx, nb, "data/ns/orders")
    assert kinds == ["input_stalled"] and "starting at the stalled offset" in facts[0]
    assert evidence.candidate_evidence(ctx, nb, "data/ns/stream")[0] == []  # not the stalled consumer
    assert evidence.candidate_evidence(ctx, nb, "configmap/ns/scripts")[0] == []  # created with the app, not changed
    assert evidence.candidate_evidence(ctx, nb, "interaction/ns/api--db")[0] == ["pointed_at"]


def test_annotate_appends_kinds_and_keeps_skipped_options():
    ctx, nb = setup()
    add(nb, 1, "cluster_objects", "quota", ["ResourceQuota ns/q: memory used 0/1Gi"])
    out = evidence.annotate(
        ctx, nb, {"cluster_object": "Another object.", "not_yet_known": "x"}, skip={"not_yet_known"}
    )
    assert out["cluster_object"].startswith("Another object. Evidence kinds: cluster_finding.")
    assert out["not_yet_known"] == "x"


def test_cluster_scoped_object_gets_its_findings_and_cluster_object_does_not():
    ctx, nb = setup()
    add(
        nb,
        1,
        "cluster_objects",
        "admission",
        [
            "Validating webhook pod-policy.example.io (created 80s before symptom onset): backend svc/x:443 with 0 ready endpoints, failurePolicy Fail"
        ],
    )
    criteria = {
        "validatingwebhookconfiguration/pod-policy.example.io": "That webhook.",
        "cluster_object": "Another object.",
    }
    out = evidence.annotate(ctx, nb, criteria, skip=set())
    assert "Evidence kinds: cluster_finding" in out["validatingwebhookconfiguration/pod-policy.example.io"]
    assert out["cluster_object"].endswith("Evidence kinds: none observed.")


def test_webhook_findings_count_for_their_own_configuration():
    # configurations created together; each line names its configuration, whose webhooks are named differently
    ctx, nb = setup()
    add(
        nb,
        1,
        "cluster_objects",
        "admission",
        [
            "MutatingWebhookConfiguration cert-webhook (created 8s before symptom onset), webhook "
            "webhook.cert.example: rules ['CREATE/UPDATE certificates']; namespaceSelector null",
            "MutatingWebhookConfiguration policy-mutating-webhook-configuration (created 8s before symptom onset), "
            "webhook mutation.policy.example: rules ['CREATE pods']; namespaceSelector null",
        ],
    )
    criteria = {
        "mutatingwebhookconfiguration/policy-mutating-webhook-configuration": "That one.",
        "mutatingwebhookconfiguration/cert-webhook": "The other one.",
        "cluster_object": "Another object.",
    }
    out = evidence.annotate(ctx, nb, criteria, skip=set())
    assert "rules ['CREATE pods']" in out["mutatingwebhookconfiguration/policy-mutating-webhook-configuration"]
    assert "CREATE pods" not in out["mutatingwebhookconfiguration/cert-webhook"]
    assert out["cluster_object"].endswith("Evidence kinds: none observed.")


def test_changes_before_the_application_existed_do_not_count(monkeypatch):
    ctx, nb = setup()
    for c in COMPS.values():
        c["created"] = "2026-10-10T00:10:00Z"
    ctx.overview = {
        "cluster": {
            "recent_changes": [
                {"object": "configmap/kube-system/coredns", "seconds_ago": 1900},
                {"object": "configmap/ns/app", "seconds_ago": 30},
            ]
        }
    }
    from datetime import datetime

    monkeypatch.setattr("clients.jev_diag.timeutil.now", lambda: datetime(2026, 10, 10, 0, 13, 20, tzinfo=UTC))
    assert evidence.candidate_evidence(ctx, nb, "configmap/kube-system/coredns")[0] == []
    assert evidence.candidate_evidence(ctx, nb, "configmap/ns/app")[0] == ["change"]


def test_object_named_like_a_workload_does_not_take_its_facts():
    ctx, nb = setup()
    add(
        nb,
        1,
        "config_and_changes",
        "deployment/db",
        [
            "fields written 30s before symptom onset: spec.replicas (db)",
            "ConfigMap db (created 90s before symptom onset; written again 20s before symptom onset (70s after creation)): x",
        ],
    )
    assert evidence.candidate_evidence(ctx, nb, "configmap/ns/db")[0] == ["change"]
    kinds, facts = evidence.candidate_evidence(ctx, nb, "configmap/ns/db")
    assert facts[0].startswith("change: ConfigMap db")
    ctx2, nb2 = setup()
    add(nb2, 1, "config_and_changes", "deployment/db", ["fields written 30s before symptom onset: spec.replicas (db)"])
    assert evidence.candidate_evidence(ctx2, nb2, "configmap/ns/db")[0] == []
    assert evidence.candidate_evidence(ctx2, nb2, "deployment/db")[0] == ["change"]


def test_caller_settings_for_a_callee_make_the_interaction_configured():
    ctx, nb = setup()
    add(nb, 1, "logs", "deployment/api", ["error lines name other components: deployment/db"])
    add(
        nb,
        2,
        "describe",
        "deployment/api",
        ["env api: DB_ADDR=db:5432, DB_RPC_TIMEOUT_MS=750, DB_MAX_ATTEMPTS=3, LOG_LEVEL=info"],
    )
    assert evidence.caller_settings_for(nb, "deployment/api", "db") == ["DB_RPC_TIMEOUT_MS=750", "DB_MAX_ATTEMPTS=3"]
    kinds, facts = evidence.candidate_evidence(ctx, nb, "interaction/ns/api--db")
    assert kinds == ["configuration", "pointed_at"] and "DB_RPC_TIMEOUT_MS=750" in facts[0]


STALE = (
    "Secret db-creds (read into environment variables) was written 40s after container api of pod api-1 started: "
    "that container still holds the values from before the write"
)


def test_stale_env_config_with_a_login_failure_counts_as_stale_config_and_its_secret_as_changed():
    ctx, nb = setup()
    add(nb, 1, "config_and_changes", "deployment/api", [STALE])
    add(nb, 2, "logs", "deployment/db", ['ongoing x5: FATAL: password authentication failed for user "app"'])
    assert evidence.candidate_evidence(ctx, nb, "deployment/api")[0][0] == "stale_config"
    assert evidence.candidate_evidence(ctx, nb, "secret/ns/db-creds")[0] == ["change"]


def test_stale_env_config_of_a_failing_component_counts_as_stale_config():
    ctx, nb = setup()
    add(nb, 1, "config_and_changes", "deployment/api", [STALE])
    add(nb, 2, "describe", "deployment/api", ["pod api-1: Running; api ready=False restarts=3"])
    assert evidence.candidate_evidence(ctx, nb, "deployment/api")[0][0] == "stale_config"


def test_routine_config_write_with_no_failure_is_only_a_change():
    # a rotated certificate or an operator's write that nothing fails on: not stale_config, which outranks all
    ctx, nb = setup()
    add(nb, 1, "config_and_changes", "deployment/api", [STALE])
    add(nb, 2, "describe", "deployment/api", ["pod api-1: Running; api ready=True restarts=0"], abnormal=0.1)
    kinds, facts = evidence.candidate_evidence(ctx, nb, "deployment/api")
    assert kinds == ["change"] and "still holds the values" in facts[0]


STALL = (
    "group g topic t partition 0: committed offset 20 unchanged over 16s while log end moved 90->98; "
    "consumer deployment/orders"
)


def test_stall_is_input_only_when_the_consumer_reports_the_offset_or_a_data_failure():
    ctx, nb = setup()
    add(nb, 1, "backend_probe", "deployment/stream", [STALL])
    assert evidence.candidate_evidence(ctx, nb, "data/ns/orders")[0] == ["symptoms"]  # logs not read yet
    add(nb, 2, "logs", "deployment/orders", ["ongoing x9: ERROR commit failed: broker connection reset"])
    assert evidence.candidate_evidence(ctx, nb, "data/ns/orders")[0] == ["symptoms"]  # a wedged consumer
    add(nb, 3, "logs", "deployment/orders", ["ongoing x9: retrying record at offset 200"])
    assert evidence.candidate_evidence(ctx, nb, "data/ns/orders")[0] == ["symptoms"]  # another offset
    add(nb, 4, "logs", "deployment/orders", ["ongoing x3: ValueError: failed to decode message payload"])
    assert evidence.candidate_evidence(ctx, nb, "data/ns/orders")[0] == ["input_stalled"]
    assert evidence.stalled_consumers(nb) == ["deployment/orders"]


def test_login_failures_found_in_ongoing_log_lines():
    ctx, nb = setup()
    add(nb, 1, "logs", "deployment/db", ['ongoing x5: FATAL: password authentication failed for user "app"'])
    add(nb, 2, "logs", "deployment/api", ["stopped x1: password authentication failed"])
    assert evidence.login_failures(nb) == ["deployment/db"]


def test_same_second_rewrite_counts_as_stale_config():
    ctx, nb = setup()
    line = (
        "Secret db-creds (read into environment variables) was rewritten in the same second as container api of pod "
        "api-1 started (timestamps have 1s resolution): that container may still hold the values from before the write"
    )
    add(nb, 1, "config_and_changes", "deployment/api", [line])
    add(nb, 2, "logs", "deployment/db", ['ongoing x5: FATAL: password authentication failed for user "app"'])
    assert evidence.candidate_evidence(ctx, nb, "deployment/api")[0][0] == "stale_config"


def _at(seconds_ago: float) -> str:
    from datetime import timedelta

    from clients.jev_diag.timeutil import now

    return (now() - timedelta(seconds=seconds_ago)).isoformat()


def test_writes_long_before_the_symptoms_are_history_not_changes():
    # a long-running application: old writes did not start symptoms that began a minute ago
    ctx, nb = setup()
    ctx.overview = {"cluster": {"symptom_onset": _at(60)}}
    assert evidence.write_note(ctx, _at(300)) == ""
    note = evidence.write_note(ctx, _at(3 * 3600))
    assert evidence.TOO_EARLY_MARK in note and "before the symptoms began" in note
    assert not evidence._is_change(f"ConfigMap app-config (created 1d ago; written again 3h ago{note}): x")
    ctx.extra["change_window_s"] = 4 * 3600  # a wider window counts it again
    assert evidence.write_note(ctx, _at(3 * 3600)) == ""


def test_overview_changes_count_only_inside_the_window():
    ctx, nb = setup()
    ctx.overview = {
        "cluster": {
            "symptom_onset": _at(60),
            "recent_changes": [
                {"object": "configmap/ns/old", "seconds_ago": 7300, "seconds_before_symptoms": 7240},
                {"object": "configmap/ns/new", "seconds_ago": 200, "seconds_before_symptoms": 140},
            ],
        }
    }
    assert evidence.candidate_evidence(ctx, nb, "configmap/ns/old")[0] == []
    assert evidence.candidate_evidence(ctx, nb, "configmap/ns/new")[0] == ["change"]


def test_any_queue_consumer_failing_again_and_again_on_one_message_is_stalled_input():
    ctx, nb = setup()
    add(
        nb,
        1,
        "logs",
        "deployment/orders",
        [
            "repeats x40 over 90s with unchanged values ['2026-10-10', 'delivery_tag=17']: "
            "2026-10-10 10:00:01 ERROR handler failed for delivery_tag=17, requeued",
        ],
    )
    kinds, facts = evidence.candidate_evidence(ctx, nb, "data/ns/orders")
    assert kinds == ["input_stalled"] and "delivery_tag=17" in facts[0]


def test_repeated_parse_failures_count_but_repeated_connection_failures_do_not():
    ctx, nb = setup()
    add(
        nb,
        1,
        "logs",
        "deployment/orders",
        [
            "repeats x30 over 60s with unchanged values ['10.0.0.5:5672']: connect to broker 10.0.0.5:5672 refused",
            "repeats x12 over 60s with unchanged values ['offset=4']: timeout fetching offset=4 from broker",
        ],
    )
    assert evidence.candidate_evidence(ctx, nb, "data/ns/orders")[0] == []
    add(
        nb,
        2,
        "logs",
        "deployment/orders",
        [
            "repeats x25 over 50s with unchanged values ['2026-10-10']: 2026-10-10 ERROR cannot deserialize payload",
        ],
    )
    assert evidence.candidate_evidence(ctx, nb, "data/ns/orders")[0] == ["input_stalled"]


def test_ready_pods_on_an_older_template_are_a_configuration_finding():
    ctx, nb = setup()
    add(
        nb,
        1,
        "describe",
        "deployment/api",
        [
            "pods of the older pod template (revision 2) still serve with settings the current template changed: "
            "pod hostAliases: [...] -> null",
        ],
    )
    assert evidence.candidate_evidence(ctx, nb, "deployment/api")[0] == ["configuration"]
