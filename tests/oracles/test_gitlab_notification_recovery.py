import copy

import pytest

from sregym.conductor.oracles.gitlab_notification_recovery import complete, notification_report

RECEIPT = {
    "project": 2,
    "iid": 12,
    "title": "Keep title",
    "description": "Keep confidential text",
    "recipient": "alice@sregym.local",
}
MAIL = {
    "id": 1,
    "recipients": ["alice@sregym.local"],
    "subject": "Project | Keep title (#12)",
    "body": "Keep confidential text\nVisit issue",
    "headers": {
        "x-gitlab-notificationreason": "sregym-ack",
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
        ("recipients", ["bob@sregym.local"]),
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
