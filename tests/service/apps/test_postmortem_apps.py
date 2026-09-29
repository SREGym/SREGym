import subprocess
from unittest.mock import Mock

import pytest

from scripts.evaluate_deathstarbench import problem_id
from sregym.service.apps.gitlab_recovery import GitLabRecovery
from sregym.service.apps.stripe_config import StripeConfig


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


def test_comparison_routes_only_supported_incident_application_pairs():
    assert problem_id("gitlab_ce", "single", "database_deletion") == "gitlab_database_deletion_single"
    assert problem_id("stripe_marathon", "replicated", "feature_config") == "stripe_feature_config_replicated"
    with pytest.raises(ValueError):
        problem_id("mattermost", "single", "feature_config")
    with pytest.raises(ValueError):
        problem_id("stripe_marathon", "expanded", "feature_config")


def test_notification_environment_preserves_real_queue_and_external_evidence():
    from sregym.service.apps.gitlab_notification_recovery import GitLabNotificationRecovery

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
    assert problem_id("gitlab_ce", "replicated", "notification_recovery") == "gitlab_notification_recovery_replicated"
    with pytest.raises(ValueError):
        problem_id("gitlab_ce", "single", "notification_recovery")


def test_ambiguous_provider_keeps_evidence_and_fault_controls_protected():
    from sregym.service.apps.gitlab_notification_ambiguity import GitLabNotificationAmbiguity

    app = GitLabNotificationAmbiguity()
    docs = app.render()
    runtime = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
    )
    assert runtime["metadata"]["labels"]["network-access"] == "restricted"
    assert set(runtime["data"]) == {"mail_sink.py", "ambiguous_mail_sink.py"}
    assert app.expected_volume_count == 8
    assert problem_id("gitlab_ce", "replicated", "notification_ambiguity") == "gitlab_notification_ambiguity_replicated"
    with pytest.raises(ValueError):
        problem_id("gitlab_ce", "single", "notification_ambiguity")


def test_intermittent_provider_preserves_protected_evidence_and_is_opt_in():
    from sregym.service.apps.gitlab_notification_intermittent import GitLabNotificationIntermittent

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
    assert (
        problem_id("gitlab_ce", "replicated", "notification_intermittent")
        == "gitlab_notification_intermittent_replicated"
    )
    with pytest.raises(ValueError):
        problem_id("gitlab_ce", "single", "notification_intermittent")
    with pytest.raises(ValueError):
        problem_id("gitea", "replicated", "notification_intermittent")


def test_delayed_provider_preserves_protected_evidence_and_is_opt_in():
    from sregym.service.apps.gitlab_notification_delayed_audit import GitLabNotificationDelayedAudit

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
    assert (
        problem_id("gitlab_ce", "replicated", "notification_delayed_audit")
        == "gitlab_notification_delayed_audit_replicated"
    )
    with pytest.raises(ValueError):
        problem_id("gitlab_ce", "single", "notification_delayed_audit")
    with pytest.raises(ValueError):
        problem_id("gitea", "replicated", "notification_delayed_audit")


def test_expanded_notifications_scale_workload_without_changing_replication_contract():
    from sregym.service.apps.gitlab_notification_expanded import GitLabNotificationExpanded

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
    assert (
        problem_id("gitlab_ce", "expanded", "notification_delayed_audit")
        == "gitlab_notification_delayed_audit_expanded"
    )
    for incident in ("database_deletion", "notification_recovery", "notification_intermittent"):
        with pytest.raises(ValueError):
            problem_id("gitlab_ce", "expanded", incident)
    with pytest.raises(ValueError):
        problem_id("gitea", "expanded", "notification_delayed_audit")
