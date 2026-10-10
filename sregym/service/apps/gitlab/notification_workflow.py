"""Reference repairs reconcile GitLab sends with public provider evidence."""

import json


def reconcile_immediate(app, receipts):
    """Reference sends real GitLab mail and checks public evidence after errors.

    Sidekiq must be stopped by the caller. Stale incident jobs are removed,
    while unrelated jobs are preserved. Accepted effects are never erased.
    """
    return app.rails(f"""
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


def reconcile_delayed(app, receipts):
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
{app.reconciliation_batches}.times do
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
    return app.rails(ruby, timeout=600)
