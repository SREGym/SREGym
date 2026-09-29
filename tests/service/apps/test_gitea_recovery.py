import copy
import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.apps.gitea_recovery import GiteaRecovery
from sregym.service.apps.gitea_recovery_workflow import reconcile_issues


@pytest.fixture
def receipt():
    return {"repository": "alice/example", "number": 3, "title": "Accepted", "body": "Acknowledged content"}


def test_replaying_receipt_twice_does_not_duplicate_an_issue(receipt):
    state = {}

    def api(path, method="GET", data=None, missing_ok=False):
        if method == "POST":
            state[path + "/3"] = {**data, "number": 3}
            return state[path + "/3"]
        return state.get(path)

    assert reconcile_issues(api, [receipt], write=True) == {"verified": 1, "created": 1}
    assert reconcile_issues(api, [receipt], write=True) == {"verified": 1, "created": 0}


def test_replay_refuses_to_overwrite_a_conflicting_issue(receipt):
    api = Mock(return_value={**receipt, "body": "Someone else's issue"})
    with pytest.raises(RuntimeError, match="not reconciled"):
        reconcile_issues(api, [receipt], write=True)
    assert api.call_count == 1


def test_replay_rejects_a_new_issue_allocated_the_wrong_number(receipt):
    api = Mock(side_effect=[None, {**receipt, "number": 7}])
    with pytest.raises(RuntimeError, match="not reconciled"):
        reconcile_issues(api, [receipt], write=True)


def test_verification_never_silently_replays_missing_work(receipt):
    api = Mock(return_value=None)
    with pytest.raises(RuntimeError, match="not reconciled"):
        reconcile_issues(api, [receipt])
    assert api.call_count == 1


def test_corrupt_archive_is_rejected_before_stopping_or_changing_production():
    app = GiteaRecovery()
    app.archive_command = Mock(side_effect=subprocess.CalledProcessError(1, ["pg_restore", "--list"]))
    app.pause_application = Mock()
    with pytest.raises(subprocess.CalledProcessError):
        app.restore_archive("/recovery/backups/latest.dump")
    app.pause_application.assert_not_called()


@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_archives_live_on_storage_independent_of_database_and_repositories(tier, members):
    app = GiteaRecovery(tier)
    docs = copy.deepcopy(app.render())
    console = next(d for d in docs if d["metadata"]["name"] == "recovery-console")
    assert console["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "gitea-recovery-archives"
    assert app.expected_volume_count == members + 2
    assert not console["spec"]["automountServiceAccountToken"]
