import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.apps.gitlab.recovery import GitLabRecoveryApplication


def test_gitlab_workload_uses_public_route_without_relaxing_readiness():
    app = GitLabRecoveryApplication()
    app.apply = Mock()

    app.start_workload()

    deployment = app.apply.call_args.args[0][0]
    source = deployment["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "http://gitlab-ce:80/users/sign_in" in source
    probes = app.probes()
    for name in ("readinessProbe", "startupProbe"):
        assert probes[name]["exec"]["command"][-1] == "http://127.0.0.1/-/readiness?all=1"


def test_bad_backup_rejected_before_application_is_stopped():
    app = GitLabRecoveryApplication()
    app.archive_command = Mock(side_effect=subprocess.CalledProcessError(1, ["pg_restore", "--list"]))
    app.command = Mock()
    with pytest.raises(subprocess.CalledProcessError):
        app.restore_archive("/recovery/backups/latest.dump")
    app.command.assert_not_called()


@pytest.mark.parametrize("variant", ["recovery", "failover", "notifications"])
def test_gitlab_restore_resets_partitioned_schemas_with_writers_stopped(variant):
    from sregym.service.apps.gitlab.failover import EAST_SNAPSHOT, GitLabFailoverApplication
    from sregym.service.apps.gitlab.notifications import GitLabNotificationApplication
    from sregym.service.apps.gitlab.recovery import ARCHIVE

    app = {
        "recovery": GitLabRecoveryApplication,
        "failover": GitLabFailoverApplication,
        "notifications": GitLabNotificationApplication,
    }[variant]()
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
        "gitlab.notification_unannounced.GitLabNotificationUnannouncedAmbiguity",
        "gitlab.notification_intermittent.GitLabNotificationIntermittent",
        "gitlab.notification_delayed_audit.GitLabNotificationDelayedAudit",
    ],
)
def test_gitlab_variants_select_final_oracles_once(monkeypatch, problem_name):
    import importlib

    import sregym.conductor.problems.gitlab.database_deletion as database_deletion
    from sregym.conductor.oracles.gitlab.notification_recovery import GitLabNotificationRecoveryOracle

    diagnosis = Mock()
    monkeypatch.setattr(database_deletion, "LLMAsAJudgeOracle", diagnosis)
    module, name = problem_name.rsplit(".", 1)
    problem = getattr(importlib.import_module("sregym.conductor.problems." + module), name)()

    diagnosis.assert_called_once_with(problem, problem.root_cause)
    assert problem.root_cause.endswith(problem.root_cause_description)
    assert isinstance(problem.mitigation_oracle, GitLabNotificationRecoveryOracle)


def test_gitlab_generated_resources_do_not_disclose_benchmark_names():
    import json

    from sregym.service.apps.gitlab.notifications import GitLabNotificationDelayedAuditApplication

    app = GitLabNotificationDelayedAuditApplication()
    assert "sregym" not in json.dumps(app.render()).lower()
    assert "sregym" not in app.client_workflow.read_text().lower()


def test_notification_environment_preserves_real_queue_and_external_evidence():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationApplication

    app = GitLabNotificationApplication()
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
    from sregym.service.apps.gitlab.notifications import GitLabNotificationAmbiguityApplication

    app = GitLabNotificationAmbiguityApplication()
    docs = app.render()
    runtime = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert set(runtime["data"]) == {"mail_sink.py", "ambiguous_mail_sink.py"}
    assert app.expected_volume_count == 8


def test_intermittent_provider_preserves_protected_evidence_and_is_opt_in():
    from sregym.service.apps.gitlab.notifications import GitLabNotificationIntermittentApplication

    app = GitLabNotificationIntermittentApplication()
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
    from sregym.service.apps.gitlab.notifications import GitLabNotificationDelayedAuditApplication

    app = GitLabNotificationDelayedAuditApplication()
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
    from sregym.service.apps.gitlab.notifications import GitLabNotificationExpandedApplication

    app = GitLabNotificationExpandedApplication()
    assert app.scale_tier == "replicated"
    assert app.recovery_counts == (20, 300, 120)
    assert GitLabRecoveryApplication("single").recovery_counts == (3, 12, 6)
    assert GitLabRecoveryApplication("replicated").recovery_counts == (5, 60, 30)
    assert app.expected_volume_count == 8
    assert app.database_document()["spec"]["instances"] == 3
    runtime = next(
        d for d in app.render() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert "delayed_mail_sink.py" in runtime["data"]


def test_failover_environment_adds_evidence_without_new_storage():
    """The divergence family reuses the archive volume; only its contents differ."""
    from sregym.service.apps.gitlab.failover import GitLabFailoverApplication

    app = GitLabFailoverApplication()
    # Same storage as the deletion family: 3 members plus 4 data volumes. The
    # notification family's 8th volume is its mailbox, which this family has not.
    assert app.expected_volume_count == GitLabRecoveryApplication().expected_volume_count == 7
    # Two full-schema restores in one attempt need more than the inherited 1 GiB.
    assert app.database_document()["spec"]["resources"]["limits"]["memory"] == "2Gi"
    # The description is architecture only; it must not advertise a briefing.
    description = app.get_app_json()["Desc"]
    assert "two regions behind a failover orchestrator" in description
    assert "failover.txt" not in description


@pytest.mark.parametrize(("tier", "projects", "orphaned", "post"), [("single", 3, 6, 6), ("replicated", 5, 30, 30)])
def test_failover_tiers_acknowledge_writes_on_both_sides_of_the_partition(tier, projects, orphaned, post):
    from sregym.service.apps.gitlab.failover import GitLabFailoverApplication

    app = GitLabFailoverApplication(tier)
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
    from sregym.service.apps.gitlab.failover import GitLabFailoverApplication

    app = GitLabFailoverApplication()
    app.archive_command = Mock(side_effect=subprocess.CalledProcessError(1, ["pg_restore", "--list"]))
    app.command = Mock()
    with pytest.raises(subprocess.CalledProcessError):
        app.restore_archive("/recovery/east/pre-partition.dump")
    app.command.assert_not_called()


RECEIPT = {"project": 4, "iid": 9, "title": "t", "description": "d", "confidential": True}


STORED = {"project_id": 4, "iid": 9, "title": "t", "description": "d", "confidential": True}


def test_reconciliation_keeps_content_and_project_and_drops_the_stale_iid():
    """The original IID belongs to a post-promotion issue and cannot be reused."""
    from sregym.service.apps.gitlab.failover import GitLabFailoverApplication

    app = GitLabFailoverApplication()
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
    from sregym.service.apps.gitlab.failover import GitLabFailoverApplication

    app = GitLabFailoverApplication()
    # Already reconciled under a fresh IID: content present, identity moved.
    app.snapshot = Mock(return_value={"issues": [{**STORED, "iid": 31}]})
    app.recovery_client = Mock()

    assert app.reconcile_orphans([RECEIPT]) == []
    app.recovery_client.assert_not_called()


def test_reconciliation_completes_a_partially_recovered_tail():
    from sregym.service.apps.gitlab.failover import GitLabFailoverApplication

    app = GitLabFailoverApplication()
    second = {**RECEIPT, "title": "u", "iid": 10}
    app.snapshot = Mock(return_value={"issues": [{**STORED, "iid": 31}]})
    app.recovery_client = Mock(return_value=[{**second, "iid": 32}])

    mapping = app.reconcile_orphans([RECEIPT, second])

    assert [r["title"] for r in app.recovery_client.call_args.kwargs["records"]] == ["u"]
    assert mapping == [{"original_iid": 10, **second, "iid": 32}]


def test_unannounced_variants_remove_the_mail_clause_and_nothing_else():
    """Description options do not change the deployed notification subsystem."""
    from sregym.service.apps.gitlab.notifications import (
        NOTIFICATION_DESCRIPTION,
        GitLabNotificationAmbiguityApplication,
        GitLabNotificationApplication,
        GitLabNotificationUnannouncedAmbiguityApplication,
        GitLabNotificationUnannouncedApplication,
    )

    for announced, unannounced in (
        (GitLabNotificationApplication, GitLabNotificationUnannouncedApplication),
        (GitLabNotificationAmbiguityApplication, GitLabNotificationUnannouncedAmbiguityApplication),
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
    from sregym.service.apps.gitlab.notifications import GitLabNotificationUnannouncedApplication
    from sregym.service.apps.gitlab.recovery import GitLabRecoveryApplication

    monkeypatch.setattr(
        GitLabRecoveryApplication,
        "get_app_json",
        lambda self: {"Name": "GitLab CE prototype", "Desc": "a description with no mail clause"},
    )
    assert (
        GitLabNotificationUnannouncedApplication("replicated").get_app_json()["Desc"]
        == "a description with no mail clause"
    )
