"""GitLab notification environments and their durable SMTP provider."""

import json
from dataclasses import dataclass
from pathlib import Path

from sregym.service.apps.gitlab.notification_workflow import reconcile_delayed, reconcile_immediate
from sregym.service.apps.gitlab.recovery import ARCHIVE, GitLabRecovery

NOTIFICATION_REASON = "issue-update"
NOTIFICATION_JOURNAL = "/recovery/journal/acknowledged-notifications.json"
NOTIFICATION_DESCRIPTION = (
    " Issue notifications are delivered over SMTP through a Sidekiq mailers queue"
    " to a local provider with a read-only delivery audit."
)


@dataclass(frozen=True)
class MailProvider:
    """Runtime files in dependency order, ending with the SMTP entry point."""

    scripts: tuple[str, ...]
    inline: bool = False

    @property
    def entrypoint(self):
        return self.scripts[-1]


MAILBOX = MailProvider(("mail_sink.py",), inline=True)
AMBIGUOUS_MAILBOX = MailProvider(("mail_sink.py", "ambiguous_mail_sink.py"))
INTERMITTENT_MAILBOX = MailProvider((*AMBIGUOUS_MAILBOX.scripts, "intermittent_mail_sink.py"))
DELAYED_MAILBOX = MailProvider((*INTERMITTENT_MAILBOX.scripts, "delayed_mail_sink.py"))


class GitLabNotificationRecovery(GitLabRecovery):
    data_volumes = (*GitLabRecovery.data_volumes, "notification-mailbox")
    auxiliary_deployments = (*GitLabRecovery.auxiliary_deployments, "notification-mailbox")
    mail_provider = MAILBOX
    describe_notifications = True
    smtp_enabled = True

    def database_document(self):
        document = super().database_document()
        # Schema restores need more memory than the generic PostgreSQL workload.
        document["spec"]["resources"]["requests"]["memory"] = "512Mi"
        document["spec"]["resources"]["limits"]["memory"] = "2Gi"
        return document

    def get_app_json(self):
        result = super().get_app_json()
        if self.describe_notifications:
            result["Desc"] += NOTIFICATION_DESCRIPTION
        return result

    def omnibus_config(self):
        config = super().omnibus_config()
        return (
            config
            + "\n"
            + "\n".join(
                f"gitlab_rails['{key}'] = {value}"
                for key, value in {
                    "smtp_address": "'notification-mailbox'",
                    "smtp_port": "1025",
                    "smtp_domain": "'example.test'",
                    "smtp_enable_starttls_auto": "false",
                    "smtp_tls": "false",
                    "gitlab_email_from": "'gitlab@example.test'",
                    "gitlab_email_reply_to": "'noreply@example.test'",
                }.items()
            )
        )

    def application_documents(self):
        provider = self.mail_provider
        directory = Path(__file__).with_name("runtime")
        sources = {name: (directory / name).read_text() for name in provider.scripts}
        volumes = [{"name": "data", "persistentVolumeClaim": {"claimName": "notification-mailbox"}}]
        mounts = [{"name": "data", "mountPath": "/data"}]
        documents = super().application_documents()
        if provider.inline:
            command = ["python", "-u", "-c", sources[provider.entrypoint]]
        else:
            command = ["python", "-u", f"/runtime/{provider.entrypoint}"]
            volumes.append({"name": "runtime", "configMap": {"name": "notification-provider-runtime"}})
            mounts.append({"name": "runtime", "mountPath": "/runtime", "readOnly": True})

        receiver = self.deployment(
            "notification-mailbox",
            {
                "name": "mailbox",
                "image": "python:3.12.13-alpine3.23",
                "command": command,
                "volumeMounts": mounts,
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}},
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
            },
            volumes,
        )
        receiver["metadata"]["labels"] = {"network-access": "restricted"}
        receiver["spec"]["template"]["metadata"]["labels"]["network-access"] = "restricted"
        service = self.service("notification-mailbox", 1025)
        service["spec"]["ports"] = [
            {"name": "smtp", "port": 1025, "targetPort": 1025},
            {"name": "audit", "port": 8080, "targetPort": 8080},
        ]
        documents.extend((service, receiver))
        if not provider.inline:
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "notification-provider-runtime", "labels": {"network-access": "restricted"}},
                    "data": sources,
                }
            )
        return documents

    def render(self):
        docs = super().render()
        for doc in docs:
            if doc["kind"] == "PersistentVolumeClaim" and doc["metadata"]["name"] == "notification-mailbox":
                doc["metadata"]["labels"] = {"network-access": "restricted"}
        return docs

    def rails(self, ruby, timeout=240):
        output = self.command(
            "exec", "-i", "deployment/gitlab-ce", "--", "gitlab-rails", "runner", "-", input_text=ruby, timeout=timeout
        )
        return json.loads(next(line[7:] for line in reversed(output.splitlines()) if line.startswith("RESULT:")))

    def control(self, verb, *services):
        for service in services:
            self.command("exec", "deployment/gitlab-ce", "--", "gitlab-ctl", verb, service, timeout=120)

    def messages(self):
        # Read the protected provider directly, independently of application routing.
        return json.loads(
            self.command(
                "exec",
                "deployment/notification-mailbox",
                "--",
                "python",
                "-c",
                "import sqlite3,json; d=sqlite3.connect('/data/mailbox.sqlite'); "
                "print(json.dumps([dict(id=r[0],**json.loads(r[1])) for r in d.execute('SELECT id,data FROM deliveries ORDER BY id')]))",
            )
        )

    def restore_archive(self, path=ARCHIVE):
        self.archive_command("pg_restore", "--list", path)
        self.control("stop", "sidekiq", "puma")
        self.restore_database(path)
        self.control("start", "puma")
        self.command("rollout", "status", "deployment/gitlab-ce", "--timeout=180s", timeout=200)
        self.wait_for_api()

    def enqueue_notification_probe(self, receipt, reason):
        return self.rails(f"""
require 'sidekiq/api'
sets = Sidekiq::Queue.all + [Sidekiq::RetrySet.new, Sidekiq::ScheduledSet.new, Sidekiq::DeadSet.new]
pending = 0
sets.each do |set|
  set.each {{ |job| pending += 1 if job.item.fetch('args', []).to_json.include?({json.dumps(NOTIFICATION_REASON)}) }}
end
Sidekiq::WorkSet.new.each {{ |_, _, work| pending += 1 if work.payload.to_json.include?({json.dumps(NOTIFICATION_REASON)}) }}
r = JSON.parse({json.dumps(json.dumps(receipt))})
issue = Issue.find_by!(project_id: r.fetch('project'), iid: r.fetch('iid'))
recipient = User.find_by!(email: r.fetch('recipient'))
job = Notify.new_issue_email(recipient.id, issue.id, {json.dumps(reason)}).deliver_later
puts 'RESULT:' + {{pending_incident_jobs: pending, job_id: job.job_id}}.to_json
""")

    def rebind_notifications(self, receipts):
        return self.rails(f"""
require 'sidekiq/api'
sets = Sidekiq::Queue.all + [Sidekiq::RetrySet.new, Sidekiq::ScheduledSet.new, Sidekiq::DeadSet.new]
removed = 0
sets.each do |set|
  set.each do |job|
    next unless job.item.fetch('args', []).to_json.include?({json.dumps(NOTIFICATION_REASON)})
    job.delete
    removed += 1
  end
end
records = JSON.parse({json.dumps(json.dumps(receipts))})
records.each do |r|
  issue = Issue.find_by!(project_id: r.fetch('project'), iid: r.fetch('iid'))
  recipient = User.find_by!(email: r.fetch('recipient'))
  Notify.new_issue_email(recipient.id, issue.id, {json.dumps(NOTIFICATION_REASON)}).deliver_later
end
puts 'RESULT:' + {{removed: removed, queued: records.length}}.to_json
""")

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
            jobs.extend(json.loads(line) for line in output.splitlines() if "issue-update" in line)
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
        message['From'] = 'digest@example.test'
        message['To'] = 'operations@example.test'
        message['Subject'] = 'Scheduled activity summary {batch}/' + str(index)
        message['X-GitLab-NotificationReason'] = 'scheduled-digest'
        message.set_content('Ordinary synthetic tenant activity; batch {batch}/' + str(index))
        smtp.send_message(message)
print('stored', {count})
""",
            timeout=180,
        )

    def ongoing_fault(self, active):
        self.command(
            "exec",
            "deployment/notification-mailbox",
            "--",
            "python",
            "-c",
            "import sys;sys.path.insert(0,'/runtime');from intermittent_mail_sink import IntermittentMailbox;"
            f"IntermittentMailbox('/data/mailbox.sqlite').set_ongoing_fault({bool(active)!r})",
        )

    def ongoing_report(self):
        return json.loads(
            self.command(
                "exec",
                "deployment/notification-mailbox",
                "--",
                "python",
                "-c",
                "import sqlite3,json;d=sqlite3.connect('/data/mailbox.sqlite');"
                "r=d.execute('SELECT active,attempts FROM ongoing_fault WHERE id=1').fetchone();"
                "print(json.dumps(dict(active=bool(r[0]),attempts=r[1])))",
            )
        )

    def publication_report(self):
        return json.loads(
            self.command(
                "exec",
                "deployment/notification-mailbox",
                "--",
                "python",
                "-c",
                "import sqlite3,json;d=sqlite3.connect('/data/mailbox.sqlite');"
                "r=d.execute('SELECT count(*),min(visible_at-accepted_at),max(visible_at-accepted_at) "
                "FROM audit_visibility WHERE visible_at>accepted_at').fetchone();"
                "p=d.execute('SELECT delay FROM audit_policy WHERE id=1').fetchone()[0];"
                "print(json.dumps(dict(delayed_deliveries=r[0],minimum_delay=r[1],maximum_delay=r[2],policy_delay=p)))",
            )
        )


class GitLabNotificationAmbiguity(GitLabNotificationRecovery):
    mail_provider = AMBIGUOUS_MAILBOX


class GitLabNotificationIntermittent(GitLabNotificationRecovery):
    mail_provider = INTERMITTENT_MAILBOX

    def deliver_reconciled_notifications(self, receipts):
        return reconcile_immediate(self, receipts)


class GitLabNotificationDelayedAudit(GitLabNotificationRecovery):
    mail_provider = DELAYED_MAILBOX
    reconciliation_batches = 4

    def deliver_reconciled_notifications(self, receipts):
        return reconcile_delayed(self, receipts)


class GitLabNotificationUnannounced(GitLabNotificationRecovery):
    describe_notifications = False


class GitLabNotificationUnannouncedAmbiguity(GitLabNotificationAmbiguity):
    describe_notifications = False


class GitLabNotificationExpanded(GitLabNotificationDelayedAudit):
    reconciliation_batches = 8

    @property
    def recovery_counts(self):
        return (20, 300, 120)

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += " Expanded workload: 20 tenant projects and 420 issues across three PostgreSQL members."
        return result

    def deploy(self):
        super().deploy()
        tenants, historical, acknowledged = self.recovery_counts
        self.archive_write(
            "/recovery/workload-scale.json",
            json.dumps(
                {
                    "workload_tier": "expanded",
                    "physical_tier": self.scale_tier,
                    "incident_tenant_projects": tenants,
                    "historical_issues": historical,
                    "acknowledged_issues": acknowledged,
                    "notification_intents": acknowledged,
                },
                indent=2,
            )
            + "\n",
        )
