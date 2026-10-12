import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.gitlab.notification_recovery import (
    GitLabNotificationRecoveryOracle,
    complete,
    notification_report,
)

RECEIPT = {
    "project": 2,
    "iid": 12,
    "title": "Keep title",
    "description": "Keep confidential text",
    "recipient": "alice@example.test",
}
MAIL = {
    "id": 1,
    "recipients": ["alice@example.test"],
    "subject": "Project | Keep title (#12)",
    "body": "Keep confidential text\nVisit issue",
    "headers": {
        "x-gitlab-notificationreason": "issue-update",
        "x-gitlab-project-id": "2",
        "x-gitlab-issue-iid": "12",
        "x-gitlab-issue-id": "9999",
    },
}


def test_public_identity_allows_safe_internal_id_changes():
    assert complete(notification_report([RECEIPT], [MAIL]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipients", ["bob@example.test"]),
        ("subject", "Different issue"),
        ("body", "Wrong private text"),
    ],
)
def test_wrong_recipient_or_content_is_sticky(field, value):
    wrong = {**MAIL, field: value, "id": 2}
    result = notification_report([RECEIPT], [wrong, MAIL])
    assert result["wrong_deliveries"] == [2]
    assert not complete(result)


def test_missing_duplicate_and_cross_project_delivery():
    assert notification_report([RECEIPT], [])["missing"] == 1
    assert notification_report([RECEIPT], [MAIL, MAIL])["duplicates"] == 1
    wrong = copy.deepcopy(MAIL)
    wrong["headers"]["x-gitlab-project-id"] = "3"
    assert notification_report([RECEIPT], [wrong])["wrong_deliveries"] == [1]


def test_unrelated_mail_is_not_counted():
    other = copy.deepcopy(MAIL)
    other["headers"]["x-gitlab-notificationreason"] = "participating"
    assert complete(notification_report([RECEIPT], [MAIL, other]))


@pytest.fixture
def later_notification():
    issue = {
        "project": 2,
        "iid": 21,
        "title": "New acknowledged issue",
        "description": "New confidential text",
        "recipients": ["alice@example.test"],
    }
    mail = copy.deepcopy(MAIL)
    mail.update(id=2, subject=issue["title"], body=issue["description"])
    mail["headers"]["x-gitlab-issue-iid"] = "21"
    # Restores may reallocate IDs, so a later issue can reuse an old internal ID.
    mail["headers"]["x-gitlab-issue-id"] = "9999"
    return issue, mail


def test_later_verified_mail_does_not_change_original_obligations(later_notification):
    issue, mail = later_notification
    new_issues = {("2", "21"): issue}
    report = notification_report([RECEIPT], [MAIL, mail], new_issues=new_issues)
    assert complete(report)
    assert report["expected"] == report["delivered"] == 1
    assert notification_report([RECEIPT], [mail], new_issues=new_issues)["missing"] == 1
    assert notification_report([RECEIPT], [MAIL, MAIL, mail], new_issues=new_issues)["duplicates"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipients", ["bob@example.test"]),
        ("recipients", ["alice@example.test", "bob@example.test"]),
        ("subject", RECEIPT["title"]),
        ("body", RECEIPT["description"]),
    ],
)
def test_later_mail_requires_real_content_and_authorized_recipient(later_notification, field, value):
    issue, mail = later_notification
    mail[field] = value
    report = notification_report([RECEIPT], [MAIL, mail], new_issues={("2", "21"): issue})
    assert report["wrong_deliveries"] == [2]


def test_unknown_mail_is_not_automatically_exempt(later_notification):
    _, mail = later_notification
    assert notification_report([RECEIPT], [MAIL, mail])["wrong_deliveries"] == [2]


def test_oracle_validates_later_mail_and_scopes_pending_jobs(monkeypatch, later_notification):
    from sregym.conductor.oracles.gitlab.database_recovery import GitLabDatabaseRecoveryOracle

    monkeypatch.setattr(GitLabDatabaseRecoveryOracle, "evaluate", lambda self: {"success": True})
    issue, mail = later_notification
    app = Mock(projects=[2])
    app.notification_issues.return_value = [issue]
    app.recovery_client.return_value = [{"project": 2, "iid": 22, "title": "Probe", "description": "Probe text"}]
    app.enqueue_notification_probe.return_value = {"pending_incident_jobs": 0}
    monkeypatch.setattr("sregym.conductor.oracles.gitlab.notification_recovery.secrets.token_hex", lambda n: "token")
    probe_mail = copy.deepcopy(MAIL)
    probe_mail.update(subject="Probe", body="Probe text", recipients=["tenant-user-1@example.test"])
    probe_mail["headers"].update(
        {"x-gitlab-issue-iid": "22", "x-gitlab-notificationreason": "notification-check-token"}
    )
    app.messages.side_effect = [[MAIL, mail], [probe_mail], [MAIL, mail]]
    original = [{"project_id": 2, "iid": 12}]
    problem = SimpleNamespace(app=app, notifications=[RECEIPT], expected={"issues": original})
    assert GitLabNotificationRecoveryOracle(problem).evaluate()["success"]
    assert app.notification_issues.call_count == 2
    app.notification_issues.assert_called_with([("2", "21")], exclude=original)
    assert app.enqueue_notification_probe.call_args.kwargs == {"incident_notifications": [RECEIPT]}
