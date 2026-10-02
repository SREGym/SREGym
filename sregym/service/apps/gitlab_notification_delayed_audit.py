"""Real GitLab recovery through ambiguous sends and delayed provider evidence."""

import json
from pathlib import Path

from sregym.service.apps.gitlab_notification_ambiguity import AUDIT_GUIDE
from sregym.service.apps.gitlab_notification_intermittent import GitLabNotificationIntermittent

#: API reference only. It must not say what is wrong, interpret a
#: symptom, or state what a correct recovery preserves.
DELAY_GUIDE = """
Provider audit fields

Each audit response includes observed_at, complete_through and
publication_delay_seconds, expressed using the provider's clock. A snapshot
contains every acceptance at or before its complete_through. Follow next to read
an entire snapshot; start again without a cursor to obtain a newer one.
"""


class GitLabNotificationDelayedAudit(GitLabNotificationIntermittent):
    reconciliation_batches = 4

    def application_documents(self):
        documents = super().application_documents()
        provider = next(
            d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "notification-mailbox"
        )
        provider["spec"]["template"]["spec"]["containers"][0]["command"] = [
            "python",
            "-u",
            "/runtime/delayed_mail_sink.py",
        ]
        runtime = next(
            d
            for d in documents
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "notification-provider-runtime"
        )
        runtime["data"]["delayed_mail_sink.py"] = (
            Path(__file__).with_name("incident_runtime") / "delayed_mail_sink.py"
        ).read_text()
        return documents

    def deploy(self):
        super().deploy()
        self.archive_write("/recovery/provider-audit.txt", AUDIT_GUIDE + DELAY_GUIDE)

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

    def deliver_reconciled_notifications(self, receipts):
        # This reference uses only the agent-visible public audit. The larger
        # reference command timeout accommodates the admitted publication tail;
        # it does not alter the agent's solving budget or the outcome grader.
        ruby = f"""
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
  path, rows, seen, snapshot, metadata = '/messages', [], [], nil, nil
  while path
    raise 'Invalid audit cursor' unless path.start_with?('/messages') && !seen.include?(path)
    seen << path
    uri = URI('http://notification-mailbox:8080' + path)
    response = Net::HTTP.start(uri.hostname, uri.port, open_timeout: 10, read_timeout: 10) {{ |http| http.get(uri.request_uri) }}
    raise 'Audit unavailable' unless response.is_a?(Net::HTTPSuccess)
    page = JSON.parse(response.body)
    snapshot ||= page.fetch('snapshot')
    raise 'Audit snapshot changed' unless snapshot == page.fetch('snapshot')
    current = page.slice('observed_at', 'complete_through', 'publication_delay_seconds')
    metadata ||= current
    raise 'Audit watermark changed within snapshot' unless metadata == current
    rows.concat(page.fetch('items'))
    path = page.fetch('next')
    total = page.fetch('total')
  end
  raise 'Incomplete provider audit' unless rows.length == total && rows.map {{ |m| m.fetch('id') }}.uniq.length == total
  metadata.merge('rows' => rows)
end

def settled_audit(first, checks)
  barrier, page = first.fetch('observed_at'), first
  started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  while page.fetch('complete_through') < barrier
    raise 'Audit publication did not converge' if Process.clock_gettime(Process::CLOCK_MONOTONIC) - started > 60
    sleep [barrier - page.fetch('complete_through'), 1.0].min
    page = provider_audit
  end
  checks << {{barrier: barrier, complete_through: page.fetch('complete_through'),
    wait_seconds: Process.clock_gettime(Process::CLOCK_MONOTONIC) - started}}
  page.fetch('rows')
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
checks, sends, delivery_errors, delayed_acceptances, batches = [], 0, [], 0, []
audit = settled_audit(provider_audit, checks)
{self.reconciliation_batches}.times do
  missing = records.reject {{ |r| accepted(audit, r) }}
  break if missing.empty?
  batches << missing.length
  # Each intent is attempted at most once in this batch. Reconcile the whole
  # batch against complete evidence before choosing the next retry set.
  missing.each do |r|
    issue = Issue.find_by!(project_id: r.fetch('project'), iid: r.fetch('iid'))
    recipient = User.find_by!(email: r.fetch('recipient'))
    raise 'Restored issue mismatch' unless issue.title == r.fetch('title') && issue.description == r.fetch('description')
    begin
      sends += 1
      Notify.new_issue_email(recipient.id, issue.id, 'sregym-ack').deliver_now
    rescue StandardError => e
      delivery_errors << {{type: e.class.name, cause: e.cause&.class&.name}}
    end
  end
  first = provider_audit
  audit = settled_audit(first, checks)
  delayed_acceptances += missing.count {{ |r| !accepted(first.fetch('rows'), r) && accepted(audit, r) }}
end
raise 'No accepted delivery after bounded reconciliation' unless records.all? {{ |r| accepted(audit, r) }}
puts 'RESULT:' + {{removed: removed, sends: sends, delivery_errors: delivery_errors,
  delayed_acceptances: delayed_acceptances, watermark_checks: checks, batches: batches,
  audit_wait_seconds: checks.sum {{ |c| c.fetch(:wait_seconds) }} }}.to_json
"""
        output = self.command(
            "exec",
            "-i",
            "deployment/gitlab-ce",
            "--",
            "gitlab-rails",
            "runner",
            "-",
            input_text=ruby,
            timeout=600,
        )
        return json.loads(
            next(line.removeprefix("RESULT:") for line in output.splitlines() if line.startswith("RESULT:"))
        )
