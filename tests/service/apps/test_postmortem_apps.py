import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.apps.gitlab.recovery import GitLabRecovery
from sregym.service.apps.stripe.configuration import StripeConfig


@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_archives_and_configuration_have_independent_storage(tier, members):
    gitlab = GitLabRecovery(tier)
    stripe = StripeConfig(tier)
    assert gitlab.expected_volume_count == members + 4
    assert stripe.expected_volume_count == members + 3
    for app in (gitlab, stripe):
        docs = app.render()
        deployments = [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"]
        assert len(deployments) == len(set(deployments))
        assert set((app.slug, *app.auxiliary_deployments)) <= set(deployments)
        assert sum(d["kind"] == "PersistentVolumeClaim" for d in docs) + members == app.expected_volume_count


def test_bad_backup_rejected_before_application_is_stopped():
    app = GitLabRecovery()
    app.archive_command = Mock(side_effect=subprocess.CalledProcessError(1, ["pg_restore", "--list"]))
    app.command = Mock()
    with pytest.raises(subprocess.CalledProcessError):
        app.restore_archive("/recovery/backups/latest.dump")
    app.command.assert_not_called()


@pytest.mark.parametrize("variant", ["recovery", "failover", "notifications"])
def test_gitlab_restore_resets_partitioned_schemas_with_writers_stopped(variant):
    from sregym.service.apps.gitlab.failover import EAST_SNAPSHOT, GitLabFailover
    from sregym.service.apps.gitlab.notifications import GitLabNotificationRecovery
    from sregym.service.apps.gitlab.recovery import ARCHIVE

    app = {"recovery": GitLabRecovery, "failover": GitLabFailover, "notifications": GitLabNotificationRecovery}[
        variant
    ]()
    calls = Mock()
    for name in ("archive_command", "command", "sql", "wait_database", "recovery_client", "control"):
        child = Mock()
        setattr(app, name, child)
        calls.attach_mock(child, name)

    app.restore_archive()

    path = EAST_SNAPSHOT if variant == "failover" else ARCHIVE
    events = calls.method_calls
    assert events[0].args == ("pg_restore", "--list", path)
    stop = events[1]
    if variant == "notifications":
        assert stop.args == ("stop", "sidekiq", "puma")
        app.control.assert_any_call("start", "puma")
        assert all("sidekiq" not in call.args for call in app.control.call_args_list[1:])
    else:
        assert stop.args == ("scale", "deployment/gitlab-ce", "--replicas=0")
        app.command.assert_any_call("wait", "--for=delete", "pod", "-l", "app=gitlab-ce", "--timeout=180s", timeout=200)
        app.command.assert_any_call("scale", "deployment/gitlab-ce", "--replicas=1")
    app.sql.assert_called_once()
    assert "DROP SCHEMA %I CASCADE" in app.sql.call_args.args[0]
    restore = app.archive_command.call_args_list[1]
    assert restore.args == (
        "pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname=gitlab_ce",
        path,
    )
    assert restore.kwargs == {"timeout": 600}
    assert next(i for i, call in enumerate(events) if call[0] == "sql") < next(
        i for i, call in enumerate(events) if call[0] == "archive_command" and "--single-transaction" in call.args
    )
    app.wait_database.assert_called_once_with()
    assert events[-1][0] == "recovery_client"
    assert events[-1].args == ("git",)


@pytest.mark.parametrize(
    "problem_name",
    [
        "gitlab_notification_unannounced.GitLabNotificationUnannouncedAmbiguity",
        "gitlab_notification_intermittent.GitLabNotificationIntermittent",
        "gitlab_notification_delayed_audit.GitLabNotificationDelayedAudit",
    ],
)
def test_gitlab_variants_select_final_oracles_once(monkeypatch, problem_name):
    import importlib

    import sregym.conductor.problems.gitlab_database_deletion as database_deletion
    from sregym.conductor.oracles.gitlab_notification_recovery import GitLabNotificationRecoveryOracle

    diagnosis = Mock()
    monkeypatch.setattr(database_deletion, "LLMAsAJudgeOracle", diagnosis)
    module, name = problem_name.rsplit(".", 1)
    problem = getattr(importlib.import_module("sregym.conductor.problems." + module), name)()

    diagnosis.assert_called_once_with(problem, problem.root_cause)
    assert problem.root_cause.endswith(problem.root_cause_description)
    assert isinstance(problem.mitigation_oracle, GitLabNotificationRecoveryOracle)


def test_gitlab_generated_resources_do_not_disclose_benchmark_names():
    import json

    from sregym.service.apps.gitlab.notifications import GitLabNotificationDelayedAudit

    app = GitLabNotificationDelayedAudit()
    assert "sregym" not in json.dumps(app.render()).lower()
    assert "sregym" not in app.client_workflow.read_text().lower()


def test_notification_environment_preserves_real_queue_and_external_evidence():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationRecovery

    app = GitLabNotificationRecovery()
    docs = app.render()
    assert app.expected_volume_count == 8
    assert app.database_document()["spec"]["resources"]["limits"]["memory"] == "2Gi"
    receiver = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "notification-mailbox")
    assert receiver["metadata"]["labels"]["network-access"] == "restricted"
    assert receiver["spec"]["template"]["metadata"]["labels"]["network-access"] == "restricted"
    storage = next(
        d for d in docs if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "notification-mailbox"
    )
    assert storage["metadata"]["labels"]["network-access"] == "restricted"
    frontend = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "gitlab-ce")
    config = frontend["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"]
    assert "gitlab_rails['smtp_address'] = 'notification-mailbox'" in config
    assert "gitlab_rails['redis_host'] = 'gitlab-redis'" in config


def test_ambiguous_provider_keeps_evidence_and_fault_controls_protected():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationAmbiguity

    app = GitLabNotificationAmbiguity()
    docs = app.render()
    runtime = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert set(runtime["data"]) == {"mail_sink.py", "ambiguous_mail_sink.py"}
    assert app.expected_volume_count == 8


def test_intermittent_provider_preserves_protected_evidence_and_is_opt_in():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationIntermittent

    app = GitLabNotificationIntermittent()
    docs = app.render()
    runtime = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert set(runtime["data"]) == {"mail_sink.py", "ambiguous_mail_sink.py", "intermittent_mail_sink.py"}
    receiver = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "notification-mailbox")
    assert receiver["metadata"]["labels"]["network-access"] == "restricted"
    assert receiver["spec"]["template"]["metadata"]["labels"]["network-access"] == "restricted"
    assert receiver["spec"]["template"]["spec"]["containers"][0]["command"][-1] == "/runtime/intermittent_mail_sink.py"
    assert app.expected_volume_count == 8


def test_delayed_provider_preserves_protected_evidence_and_is_opt_in():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationDelayedAudit

    app = GitLabNotificationDelayedAudit()
    docs = app.render()
    runtime = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert set(runtime["data"]) == {
        "mail_sink.py",
        "ambiguous_mail_sink.py",
        "intermittent_mail_sink.py",
        "delayed_mail_sink.py",
    }
    receiver = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "notification-mailbox")
    assert receiver["spec"]["template"]["metadata"]["labels"]["network-access"] == "restricted"
    assert receiver["spec"]["template"]["spec"]["containers"][0]["command"][-1] == "/runtime/delayed_mail_sink.py"
    assert app.expected_volume_count == 8


def test_expanded_notifications_scale_workload_without_changing_replication_contract():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationExpanded

    app = GitLabNotificationExpanded()
    assert app.scale_tier == "replicated"
    assert app.recovery_counts == (20, 300, 120)
    assert GitLabRecovery("single").recovery_counts == (3, 12, 6)
    assert GitLabRecovery("replicated").recovery_counts == (5, 60, 30)
    assert app.expected_volume_count == 8
    assert app.database_document()["spec"]["instances"] == 3
    runtime = next(
        d for d in app.render() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert "delayed_mail_sink.py" in runtime["data"]


def test_failover_environment_adds_evidence_without_new_storage():
    """The divergence family reuses the archive volume; only its contents differ."""
    from sregym.service.apps.gitlab.failover import GitLabFailover

    app = GitLabFailover()
    # Same storage as the deletion family: 3 members plus 4 data volumes. The
    # notification family's 8th volume is its mailbox, which this family has not.
    assert app.expected_volume_count == GitLabRecovery().expected_volume_count == 7
    # Two full-schema restores in one attempt need more than the inherited 1 GiB.
    assert app.database_document()["spec"]["resources"]["limits"]["memory"] == "2Gi"
    # The description is architecture only; it must not advertise a briefing.
    description = app.get_app_json()["Desc"]
    assert "two regions behind a failover orchestrator" in description
    assert "failover.txt" not in description


@pytest.mark.parametrize(("tier", "projects", "orphaned", "post"), [("single", 3, 6, 6), ("replicated", 5, 30, 30)])
def test_failover_tiers_acknowledge_writes_on_both_sides_of_the_partition(tier, projects, orphaned, post):
    from sregym.service.apps.gitlab.failover import GitLabFailover

    app = GitLabFailover(tier)
    assert app.recovery_counts[0] == projects
    assert app.recovery_counts[2] == orphaned
    assert app.divergence_counts == (orphaned, post)
    # Both tails must be non-empty, or the incident has no divergence to recover.
    assert orphaned and post


def test_the_failover_family_ships_no_guide():
    """This test previously *required* the leak.

    It asserted the guide stated which side keeps its IIDs -- the recovery
    contract, which is the judgement the family exists to test. Removing that
    one disclosure took the family from 3 of 3 solved to 1 of 3.
    """
    import sregym.service.apps.gitlab.failover as failover

    assert not hasattr(failover, "FAILOVER_GUIDE")
    # The journal still exists: it is the evidence, not a briefing about it.
    assert failover.PARTITION_JOURNAL.endswith("partition-acknowledged.json")


def test_failover_rejects_a_bad_snapshot_before_stopping_the_application():
    from sregym.service.apps.gitlab.failover import GitLabFailover

    app = GitLabFailover()
    app.archive_command = Mock(side_effect=subprocess.CalledProcessError(1, ["pg_restore", "--list"]))
    app.command = Mock()
    with pytest.raises(subprocess.CalledProcessError):
        app.restore_archive("/recovery/east/pre-partition.dump")
    app.command.assert_not_called()


RECEIPT = {"project": 4, "iid": 9, "title": "t", "description": "d", "confidential": True}
STORED = {"project_id": 4, "iid": 9, "title": "t", "description": "d", "confidential": True}


def test_reconciliation_keeps_content_and_project_and_drops_the_stale_iid():
    """The original IID belongs to a post-promotion issue and cannot be reused."""
    from sregym.service.apps.gitlab.failover import GitLabFailover

    app = GitLabFailover()
    app.snapshot = Mock(return_value={"issues": []})
    app.recovery_client = Mock(
        return_value=[{"project": 4, "title": "t", "description": "d", "confidential": True, "iid": 31}]
    )

    mapping = app.reconcile_orphans([RECEIPT])

    assert app.recovery_client.call_args.kwargs["records"] == [
        {"project": 4, "title": "t", "description": "d", "confidential": True}
    ]
    assert mapping == [
        {"original_iid": 9, "project": 4, "title": "t", "description": "d", "confidential": True, "iid": 31}
    ]


def test_reconciliation_skips_writes_already_present_exactly_once():
    """Cleanup after a partly successful attempt must not create duplicates."""
    from sregym.service.apps.gitlab.failover import GitLabFailover

    app = GitLabFailover()
    # Already reconciled under a fresh IID: content present, identity moved.
    app.snapshot = Mock(return_value={"issues": [{**STORED, "iid": 31}]})
    app.recovery_client = Mock()

    assert app.reconcile_orphans([RECEIPT]) == []
    app.recovery_client.assert_not_called()


def test_reconciliation_completes_a_partially_recovered_tail():
    from sregym.service.apps.gitlab.failover import GitLabFailover

    app = GitLabFailover()
    second = {**RECEIPT, "title": "u", "iid": 10}
    app.snapshot = Mock(return_value={"issues": [{**STORED, "iid": 31}]})
    app.recovery_client = Mock(return_value=[{**second, "iid": 32}])

    mapping = app.reconcile_orphans([RECEIPT, second])

    assert [r["title"] for r in app.recovery_client.call_args.kwargs["records"]] == ["u"]
    assert mapping == [{"original_iid": 10, **second, "iid": 32}]


def test_cascade_gateway_runs_several_replicas_and_is_not_oracle_topology_checked():
    """The shared SaaS oracle demands exactly one replica of its deployments.

    The gateway is meant to run several, and deleting the scaler is a legitimate
    repair, so neither may be an `auxiliary_deployment`.
    """
    from sregym.service.apps.mattermost.cascade import MattermostCascade

    app = MattermostCascade()
    docs = app.render()
    assert set(app.cascade_deployments).isdisjoint(app.auxiliary_deployments)
    gateway = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "chat-gateway")
    assert gateway["spec"]["replicas"] == app.capacity_floor >= 2
    # A recreate strategy would drop the whole gateway on every scale change.
    assert gateway["spec"]["strategy"]["type"] == "RollingUpdate"


def test_only_the_scaler_gets_an_api_token_and_only_over_the_gateway_scale():
    from sregym.service.apps.mattermost.cascade import MattermostCascade

    docs = MattermostCascade().render()
    pods = [d for d in docs if d["kind"] == "Deployment"]
    tokened = [d["metadata"]["name"] for d in pods if d["spec"]["template"]["spec"].get("automountServiceAccountToken")]
    assert tokened == ["capacity-scaler"]
    role = next(d for d in docs if d["kind"] == "Role")
    scale_rule = next(r for r in role["rules"] if r["resources"] == ["deployments/scale"])
    assert scale_rule["resourceNames"] == ["chat-gateway"]
    assert sorted(scale_rule["verbs"]) == ["get", "patch", "update"]
    # No write access to anything else, and no access to secrets at all.
    assert not any("secrets" in r["resources"] for r in role["rules"])


def test_cascade_control_state_is_persistent_so_a_restart_cannot_clear_it():
    from sregym.service.apps.mattermost.cascade import CONTROL_VOLUME, MattermostCascade

    app = MattermostCascade()
    assert CONTROL_VOLUME in app.data_volumes
    docs = app.render()
    assert any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == CONTROL_VOLUME for d in docs)
    for name in ("chat-gateway", "capacity-scaler"):
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
        volumes = deployment["spec"]["template"]["spec"]["volumes"]
        assert any(v.get("persistentVolumeClaim", {}).get("claimName") == CONTROL_VOLUME for v in volumes), name


def test_customer_traffic_is_concurrent_and_part_of_the_environment():
    """A sequential probe can never saturate a worker pool, however slow it gets."""
    from sregym.service.apps.mattermost.cascade import MattermostCascade

    app = MattermostCascade()
    docs = app.render()
    traffic = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "chat-traffic")
    source = traffic["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "threading.Thread" in source
    assert "chat-gateway:8080" in source
    # More concurrent clients than the gateway has workers across the floor.
    assert f"range({app.gateway_workers * app.capacity_floor})" in source
    # The conductor's inherited sequential workload would only muddy the signal.
    assert app.start_workload() is None


def test_the_cascade_family_ships_no_guide():
    """This test previously required the leak too.

    It asserted the guide stated that capacity taken away again is not recovery,
    and pointed at the metrics endpoint and the latency control file. The agent's
    first action on the old screen was to read that file and then execute it.
    """
    import sregym.service.apps.mattermost.cascade as cascade

    assert not hasattr(cascade, "CASCADE_GUIDE")
    description = cascade.MattermostCascade().get_app_json()["Desc"]
    assert "chat-gateway" in description
    assert "README" not in description


def test_calibrated_policy_still_reads_a_saturated_gateway_as_idle():
    """Thresholds come from measured healthy CPU, so they travel between hosts."""
    from unittest.mock import Mock

    from sregym.service.apps.mattermost.cascade import MattermostCascade
    from sregym.service.apps.mattermost.runtime.capacity_scaler import DEFAULT_POLICY, decide

    app = MattermostCascade()
    app.healthy_cpu = Mock(return_value=44.0)
    written = {}
    app.write_control = Mock(side_effect=lambda name, content: written.__setitem__(name, content))

    policy = app.calibrate_scaler()

    assert policy["scale_in_below"] == 22
    assert policy["calibrated_healthy_cpu_percent"] == 44.0
    # Calibration is also what turns the automation on; before it, the scaler has
    # no policy file and deliberately does nothing.
    assert policy["enabled"] is True
    # Healthy load sits inside the band; a blocked pool falls below it.
    rules = {**DEFAULT_POLICY, **policy}
    assert decide(3, 44.0, rules, "cpu")[0] == 3
    assert decide(3, 2.0, rules, "cpu")[0] == 2
    assert "healthy gateway CPU: 44.0%" in written["capacity-policy.txt"]


def test_unannounced_variants_remove_the_mail_clause_and_nothing_else():
    """Description options do not change the deployed notification subsystem."""
    from sregym.service.apps.gitlab.notifications import (
        NOTIFICATION_DESCRIPTION,
        GitLabNotificationAmbiguity,
        GitLabNotificationRecovery,
        GitLabNotificationUnannounced,
        GitLabNotificationUnannouncedAmbiguity,
    )

    for announced, unannounced in (
        (GitLabNotificationRecovery, GitLabNotificationUnannounced),
        (GitLabNotificationAmbiguity, GitLabNotificationUnannouncedAmbiguity),
    ):
        loud = announced("replicated").get_app_json()["Desc"]
        quiet = unannounced("replicated").get_app_json()["Desc"]
        assert NOTIFICATION_DESCRIPTION in loud
        assert NOTIFICATION_DESCRIPTION not in quiet
        assert quiet == loud.replace(NOTIFICATION_DESCRIPTION, "")
        # The subsystem itself must still be there: this withholds narration,
        # not capability. An unsolvable task is not a harder task.
        documents = unannounced("replicated").render()
        names = [d["metadata"]["name"] for d in documents]
        assert "notification-mailbox" in names
        assert names.count("notification-mailbox") == 3  # PVC, Service, Deployment


def test_description_options_do_not_depend_on_exact_parent_wording(monkeypatch):
    from sregym.service.apps.gitlab.notifications import GitLabNotificationUnannounced
    from sregym.service.apps.gitlab.recovery import GitLabRecovery

    monkeypatch.setattr(
        GitLabRecovery,
        "get_app_json",
        lambda self: {"Name": "GitLab CE prototype", "Desc": "a description with no mail clause"},
    )
    assert GitLabNotificationUnannounced("replicated").get_app_json()["Desc"] == "a description with no mail clause"


@pytest.mark.parametrize(
    "application",
    [
        "sregym.service.apps.gitlab.notifications.GitLabNotificationUnannouncedAmbiguity",
        "sregym.service.apps.gitlab.notifications.GitLabNotificationIntermittent",
        "sregym.service.apps.gitlab.notifications.GitLabNotificationDelayedAudit",
        "sregym.service.apps.stripe.configuration.StripeConfig",
    ],
)
def test_agent_descriptions_do_not_state_the_diagnosis_or_recovery_procedure(application):
    import importlib

    module, name = application.rsplit(".", 1)
    app = getattr(importlib.import_module(module), name)()
    description = app.get_app_json()["Desc"]
    for phrase in (
        "Recover every intended notification exactly once",
        "without losing acknowledged",
        "Check candidate archives before use",
        "does not establish that a send was rejected",
        "take thirty seconds to appear",
    ):
        assert phrase not in description
