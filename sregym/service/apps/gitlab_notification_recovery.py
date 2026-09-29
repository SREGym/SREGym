"""GitLab's real Sidekiq mail queue, with a local durable SMTP recipient."""

import json
import time
from pathlib import Path

from sregym.service.apps.gitlab_recovery import ARCHIVE, GitLabRecovery

NOTIFICATION_REASON = "sregym-ack"
NOTIFICATION_JOURNAL = "/recovery/journal/acknowledged-notifications.json"
MAIL_GUIDE = """
Notification recovery contract

The SMTP provider has already accepted some customer notifications; others were
acknowledged by GitLab but remain queued. Recover every intended notification
exactly once, to its intended recipient, with the correct issue content. An email
cannot be unsent. Database availability alone does not complete this incident.
Fresh issue notifications must also work after recovery.

The notification journal records the acknowledged intent, including project ID,
public issue IID, recipient and queue job ID. Redis and the recipient mailbox
survive database loss independently. The external provider's read-only audit is
http://notification-mailbox:8080/messages. It accepts local synthetic mail only;
its retained delivery history is external evidence and must not be modified.
GitLab uses its ordinary Notify mailer and Sidekiq mailers queue. The journal's
notification reason distinguishes incident work from unrelated mail.
"""


class GitLabNotificationRecovery(GitLabRecovery):
    data_volumes = (*GitLabRecovery.data_volumes, "notification-mailbox")
    auxiliary_deployments = (*GitLabRecovery.auxiliary_deployments, "notification-mailbox")

    def database_document(self):
        document = super().database_document()
        # Repeated full-schema restores retain substantial catalog/lock state.
        # The generic 1 GiB cap can OOM PostgreSQL during this recovery workload.
        document["spec"]["resources"]["requests"]["memory"] = "512Mi"
        document["spec"]["resources"]["limits"]["memory"] = "2Gi"
        return document

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += " Recover acknowledged notifications exactly once; see /recovery/notifications.txt."
        return result

    def application_documents(self):
        docs = super().application_documents()
        gitlab = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == self.slug)
        config = gitlab["spec"]["template"]["spec"]["containers"][0]["env"][0]
        config["value"] = (
            config["value"]
            .replace("['smtp_enable'] = false", "['smtp_enable'] = true")
            .replace("['gitlab_email_enabled'] = false", "['gitlab_email_enabled'] = true")
        )
        config["value"] += "\n" + "\n".join(
            f"gitlab_rails['{key}'] = {value}"
            for key, value in {
                "smtp_address": "'notification-mailbox'",
                "smtp_port": "1025",
                "smtp_domain": "'sregym.local'",
                "smtp_enable_starttls_auto": "false",
                "smtp_tls": "false",
                "gitlab_email_from": "'gitlab@sregym.local'",
                "gitlab_email_reply_to": "'noreply@sregym.local'",
            }.items()
        )
        source = Path(__file__).with_name("incident_runtime").joinpath("mail_sink.py").read_text()
        receiver = self.deployment(
            "notification-mailbox",
            {
                "name": "mailbox",
                "image": "python:3.12.13-alpine3.23",
                "command": ["python", "-u", "-c", source],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}},
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
            },
            [{"name": "data", "persistentVolumeClaim": {"claimName": "notification-mailbox"}}],
        )
        receiver["metadata"]["labels"] = {"network-access": "restricted"}
        receiver["spec"]["template"]["metadata"]["labels"]["network-access"] = "restricted"
        service = self.service("notification-mailbox", 1025)
        service["spec"]["ports"] = [
            {"name": "smtp", "port": 1025, "targetPort": 1025},
            {"name": "audit", "port": 8080, "targetPort": 8080},
        ]
        return [*docs, service, receiver]

    def render(self):
        docs = super().render()
        for doc in docs:
            if doc["kind"] == "PersistentVolumeClaim" and doc["metadata"]["name"] == "notification-mailbox":
                doc["metadata"]["labels"] = {"network-access": "restricted"}
        return docs

    def deploy(self):
        super().deploy()
        self.archive_write("/recovery/notifications.txt", MAIL_GUIDE)

    def rails(self, ruby):
        output = self.command(
            "exec", "-i", "deployment/gitlab-ce", "--", "gitlab-rails", "runner", "-", input_text=ruby, timeout=240
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
        # GitLab has inherited/partitioned constraints that pg_restore --clean
        # cannot individually drop on an already restored database. Rebuild the
        # schemas while writers are quiesced, just as on the initial empty DB.
        self.sql("""DO $$ DECLARE n text; BEGIN
          FOR n IN SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
            AND nspname <> 'information_schema' LOOP
            EXECUTE format('DROP SCHEMA %I CASCADE', n);
          END LOOP;
          CREATE SCHEMA public AUTHORIZATION gitlab_ce;
        END $$;""")
        self.archive_command(
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname=gitlab_ce",
            path,
            timeout=600,
        )
        self.wait_database()
        self.control("start", "puma")
        self.command("rollout", "status", "deployment/gitlab-ce", "--timeout=180s", timeout=200)
        # Readiness status can briefly remain True while Puma starts.
        deadline = time.monotonic() + 180
        while True:
            try:
                self.recovery_client("git")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

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
