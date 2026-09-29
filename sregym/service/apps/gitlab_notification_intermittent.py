"""GitLab recovery while SMTP delivery outcomes remain unreliable."""

import json
from pathlib import Path

from sregym.service.apps.gitlab_notification_ambiguity import GitLabNotificationAmbiguity


class GitLabNotificationIntermittent(GitLabNotificationAmbiguity):
    def application_documents(self):
        documents = super().application_documents()
        provider = next(
            d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "notification-mailbox"
        )
        provider["spec"]["template"]["spec"]["containers"][0]["command"] = [
            "python",
            "-u",
            "/runtime/intermittent_mail_sink.py",
        ]
        runtime = next(
            d
            for d in documents
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
        )
        runtime["data"]["intermittent_mail_sink.py"] = (
            Path(__file__).with_name("incident_runtime") / "intermittent_mail_sink.py"
        ).read_text()
        return documents

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

    def deliver_reconciled_notifications(self, receipts):
        """Reference sends real GitLab mail and checks public evidence after errors.

        Sidekiq must be stopped by the caller. Stale incident jobs are removed,
        while unrelated jobs are preserved. Accepted effects are never erased.
        """
        return self.rails(f"""
require 'sidekiq/api'
require 'net/http'
require 'json'
sets = Sidekiq::Queue.all + [Sidekiq::RetrySet.new, Sidekiq::ScheduledSet.new, Sidekiq::DeadSet.new]
removed = 0
sets.each do |set|
  set.each do |job|
    next unless job.item.fetch('args', []).to_json.include?('sregym-ack')
    job.delete
    removed += 1
  end
end

def provider_audit
  path, rows, seen, snapshot = '/messages', [], [], nil
  while path
    raise 'Invalid audit cursor' unless path.start_with?('/messages') && !seen.include?(path)
    seen << path
    uri = URI('http://notification-mailbox:8080' + path)
    response = Net::HTTP.start(uri.hostname, uri.port, open_timeout: 10, read_timeout: 10) {{ |http| http.get(uri.request_uri) }}
    raise 'Audit unavailable' unless response.is_a?(Net::HTTPSuccess)
    page = JSON.parse(response.body)
    snapshot ||= page.fetch('snapshot')
    raise 'Audit snapshot changed' unless snapshot == page.fetch('snapshot')
    rows.concat(page.fetch('items'))
    path = page.fetch('next')
    total = page.fetch('total')
  end
  raise 'Incomplete provider audit' unless rows.length == total && rows.map {{ |m| m.fetch('id') }}.uniq.length == total
  rows
end

def accepted(rows, r)
  rows.any? do |m|
    h = m.fetch('headers')
    h['x-gitlab-notificationreason'] == 'sregym-ack' &&
      h['x-gitlab-project-id'] == r.fetch('project').to_s && h['x-gitlab-issue-iid'] == r.fetch('iid').to_s &&
      m.fetch('recipients') == [r.fetch('recipient')] && m.fetch('subject').include?(r.fetch('title')) &&
      m.fetch('body').include?(r.fetch('description'))
  end
end

records = JSON.parse({json.dumps(json.dumps(receipts))})
audit, sends, delivery_errors = provider_audit, 0, []
records.each do |r|
  next if accepted(audit, r)
  issue = Issue.find_by!(project_id: r.fetch('project'), iid: r.fetch('iid'))
  recipient = User.find_by!(email: r.fetch('recipient'))
  raise 'Restored issue mismatch' unless issue.title == r.fetch('title') && issue.description == r.fetch('description')
  4.times do
    break if accepted(audit, r)
    begin
      sends += 1
      Notify.new_issue_email(recipient.id, issue.id, 'sregym-ack').deliver_now
    rescue StandardError => e
      # A mail-stack exception can follow an accepted external effect. The
      # audit decides whether retry is safe, regardless of exception wrapping.
      # Persistent rendering/application failures still fail the bounded loop.
      delivery_errors << {{type: e.class.name, cause: e.cause&.class&.name}}
    end
    # Never infer delivery from a transport exception or a missing SMTP reply.
    audit = provider_audit
  end
  raise 'No accepted delivery after bounded reconciliation' unless accepted(audit, r)
end
puts 'RESULT:' + {{removed: removed, sends: sends, delivery_errors: delivery_errors}}.to_json
""")
