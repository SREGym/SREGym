"""GitLab recovery with transport-ambiguous mail and a noisy provider audit."""

import json
from pathlib import Path

from sregym.service.apps.gitlab_notification_recovery import GitLabNotificationRecovery

#: API reference only. It must not say what is wrong, interpret a
#: symptom, or state what a correct recovery preserves.
AUDIT_GUIDE = """
Provider audit API

GET http://notification-mailbox:8080/messages returns an object containing
items, next, snapshot and total. Each response contains at most 40 messages.
Follow the relative next URL until it is null; subsequent pages retain the same
snapshot. Begin a new request without a cursor to observe later deliveries.
The audit includes unrelated local synthetic mail. There are no audit mutation
endpoints.
"""


class GitLabNotificationAmbiguity(GitLabNotificationRecovery):
    def application_documents(self):
        documents = super().application_documents()
        provider = next(
            d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "notification-mailbox"
        )
        container = provider["spec"]["template"]["spec"]["containers"][0]
        container["command"] = ["python", "-u", "/runtime/ambiguous_mail_sink.py"]
        container["volumeMounts"].append({"name": "runtime", "mountPath": "/runtime", "readOnly": True})
        provider["spec"]["template"]["spec"]["volumes"].append(
            {"name": "runtime", "configMap": {"name": "notification-provider-runtime"}}
        )
        directory = Path(__file__).with_name("incident_runtime")
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "notification-provider-runtime", "labels": {"network-access": "restricted"}},
                "data": {name: (directory / name).read_text() for name in ("mail_sink.py", "ambiguous_mail_sink.py")},
            }
        )
        return documents

    def deploy(self):
        super().deploy()
        # Keep the outcome contract, but explicitly replace the old list-only API.
        self.archive_write("/recovery/provider-audit.txt", AUDIT_GUIDE)
        self.archive_command(
            "sh",
            "-c",
            "printf '\\nThe provider audit is paginated; see /recovery/provider-audit.txt.\\n' >> /recovery/notifications.txt",
        )

    def provider_fault(self, remaining=None):
        self.command(
            "exec",
            "deployment/notification-mailbox",
            "--",
            "python",
            "-c",
            "import sys;sys.path.insert(0,'/runtime');from ambiguous_mail_sink import AmbiguousMailbox;"
            f"AmbiguousMailbox('/data/mailbox.sqlite').set_fault({remaining!r})",
        )

    def transport_report(self):
        return json.loads(
            self.command(
                "exec",
                "deployment/notification-mailbox",
                "--",
                "python",
                "-c",
                "import sqlite3,json; d=sqlite3.connect('/data/mailbox.sqlite'); "
                "r=d.execute('SELECT count(delivery_id),count(*)-count(delivery_id) FROM transport_faults').fetchone();"
                "print(json.dumps(dict(accepted_without_ack=r[0],failed_without_acceptance=r[1])))",
            )
        )

    def incident_jobs(self):
        jobs = []
        for key in ("queue:mailers", "retry", "schedule", "dead"):
            command = "LRANGE" if key.startswith("queue:") else "ZRANGE"
            output = self.command(
                "exec", "deployment/gitlab-redis", "--", "redis-cli", "--raw", command, key, "0", "-1"
            )
            jobs.extend(json.loads(line) for line in output.splitlines() if "sregym-ack" in line)
        return jobs

    def public_messages(self):
        """Reference evidence uses the same paginated HTTP surface as the agent."""
        return json.loads(
            self.command(
                "exec",
                "-i",
                "application-client",
                "--",
                "python",
                "-",
                input_text="""
import json, urllib.request
base = 'http://notification-mailbox:8080'
path, messages, seen = '/messages', [], set()
snapshot = None
while path is not None:
    assert path.startswith('/messages') and path not in seen
    seen.add(path)
    page = json.load(urllib.request.urlopen(base + path, timeout=10))
    snapshot = page['snapshot'] if snapshot is None else snapshot
    assert page['snapshot'] == snapshot
    messages.extend(page['items'])
    path = page['next']
assert len(messages) == page['total']
assert len({m['id'] for m in messages}) == len(messages)
print(json.dumps(messages))
""",
            )
        )

    def add_audit_noise(self, count, batch):
        self.command(
            "exec",
            "-i",
            "application-client",
            "--",
            "python",
            "-",
            input_text=f"""
import smtplib
from email.message import EmailMessage
with smtplib.SMTP('notification-mailbox', 1025, timeout=20) as smtp:
    for index in range({count}):
        message = EmailMessage()
        message['From'] = 'digest@sregym.local'
        message['To'] = 'operations@sregym.local'
        message['Subject'] = 'Scheduled activity summary {batch}/' + str(index)
        message['X-GitLab-NotificationReason'] = 'scheduled-digest'
        message.set_content('Ordinary synthetic tenant activity; batch {batch}/' + str(index))
        smtp.send_message(message)
print('stored', {count})
""",
            timeout=180,
        )
