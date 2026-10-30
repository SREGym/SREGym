import pytest

from sregym.service.apps.gitlab.recovery import GitLabRecoveryApplication
from sregym.service.apps.stripe.configuration import StripeConfigApplication

pytestmark = pytest.mark.usefixtures("offline_kubectl")


@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_archives_and_configuration_have_independent_storage(tier, members):
    gitlab = GitLabRecoveryApplication(tier)
    stripe = StripeConfigApplication(tier)
    assert gitlab.expected_volume_count == members + 4
    assert stripe.expected_volume_count == members + 3
    for app in (gitlab, stripe):
        docs = app.render()
        deployments = [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"]
        assert len(deployments) == len(set(deployments))
        assert set((app.slug, *app.auxiliary_deployments)) <= set(deployments)
        assert sum(d["kind"] == "PersistentVolumeClaim" for d in docs) + members == app.expected_volume_count


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
