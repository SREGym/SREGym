"""Backlog for Frappe."""

from __future__ import annotations

import json

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.apps.frappe.faults import FrappeLeg
from sregym.service.apps.frappe.application import Frappe
from sregym.service.apps.frappe.operations import site_database

MAIL_SINK_MANIFEST = Frappe.metadata_file.parent / "fixtures" / "mail-sink.yaml"


_SEED_BACKLOG = """
import json, zlib
from frappe.utils.background_jobs import get_queue
reports = []
for _ in range(2):
    doc = frappe.get_doc({"doctype": "Prepared Report", "report_name": REPORT, "filters": "{}"})
    doc.insert(ignore_permissions=True)
    reports.append(doc.name)
if not frappe.db.exists("Email Account", "Outbound relay"):
    frappe.get_doc({
        "doctype": "Email Account", "email_account_name": "Outbound relay",
        "email_id": "notifications@example.com", "enable_outgoing": 1, "default_outgoing": 1,
        "smtp_server": "mail-sink", "smtp_port": 1025, "use_tls": 0, "use_ssl_for_outgoing": 0,
        "no_smtp_authentication": 1,
    }).insert(ignore_permissions=True)
mails, subjects = [], []
for i in range(4):
    subject = "Quarterly access review reminder %d (%s)" % (i + 1, frappe.generate_hash(length=8))
    queued = frappe.sendmail(
        recipients=["reviewer%d@example.com" % (i + 1)], sender="notifications@example.com",
        subject=subject, message="Please complete your quarterly access review.", now=False,
    )
    frappe.enqueue(
        "frappe.email.doctype.email_queue.email_queue.send_now",
        queue="long", name=queued.name, enqueue_after_commit=True,
    )
    mails.append(queued.name)
    subjects.append(subject)
frappe.db.commit()
queue = get_queue("long")
conn = queue.connection
stranded = None
for job_id in conn.lrange(queue.key, 0, -1):
    data = conn.hget(b"rq:job:" + job_id, "data")
    try:
        payload = zlib.decompress(data or b"")
    except zlib.error:
        continue
    if b"generate_report" in payload and reports[0].encode() in payload:
        conn.lrem(queue.key, 0, job_id)
        stranded = job_id.decode()
        break
print(json.dumps({"reports": reports, "mails": mails, "subjects": subjects, "stranded_job": stranded}))
"""


class StrandedBacklogSeed(FrappeLeg):
    """Background work accepted before the outage: two Prepared Reports and four emails.

    Mirrors Incident Arena's ``stranded_prepared_report_with_emails`` seed. It
    runs while the queue user cannot dequeue, so every job waits in Redis; one
    report's job id is then dropped from the ``long`` queue list (its job hash
    stays ``queued``), so repairing the broker alone never completes it. Each
    email must reach the in-cluster mailbox exactly once.
    """

    component = "redis-queue/redis-queue.acl"
    REPORT = "Database Storage Usage By Tables"

    def __init__(self) -> None:
        super().__init__()
        self.seed: dict = {}

    def inject(self) -> None:
        app = self.frappe
        app.kubectl.apply_configs(app.namespace, str(MAIL_SINK_MANIFEST))
        app.wait_rollout("deployment", "mail-sink", 300)
        out = app.site_python(f"REPORT = {self.REPORT!r}\n" + _SEED_BACKLOG)
        self.seed = json.loads(out.strip().splitlines()[-1])
        if not self.seed.get("stranded_job"):
            raise RuntimeError(f"could not strand a Prepared Report job: {self.seed}")

    def recover(self) -> None:
        """Re-deliver the stranded report job (the reference repair's reconciliation step).

        Runs after the queue leg is repaired; the mail sink stays up so the
        queued emails can still be delivered (namespace teardown removes it).
        """
        job_id = self.seed.get("stranded_job")
        if not job_id:
            return
        self.frappe.site_python(
            "from frappe.utils.background_jobs import get_queue\n"
            "queue = get_queue('long')\n"
            "conn = queue.connection\n"
            f"job = {job_id!r}.encode()\n"
            "if job not in conn.lrange(queue.key, 0, -1) and conn.hget(b'rq:job:' + job, 'status') == b'queued':\n"
            "    conn.lpush(queue.key, job)\n"
            "print('redelivered')\n"
        )

    def _report_states(self) -> dict[str, str]:
        names = ",".join(f"'{n}'" for n in self.seed.get("reports", []))
        if not names:
            return {}
        database = site_database(self.frappe)
        rows = self.frappe.mysql(f"SELECT name, status FROM `{database}`.`tabPrepared Report` WHERE name IN ({names})")
        return dict(line.split("\t", 1) for line in rows.splitlines() if "\t" in line)

    def _delivered_subjects(self) -> list[str]:
        status, body = self.frappe.http("http://mail-sink:8025/api/v1/messages?limit=500")
        if status != 200:
            raise RuntimeError(f"mail sink returned {status}")
        return [m.get("Subject", "") for m in json.loads(body).get("messages", [])]

    def checks(self, phase: str) -> list[CheckResult]:
        if phase != "soak_end" or not self.seed:
            return []
        states = self._report_states()
        delivered = self._delivered_subjects()
        counts = {s: delivered.count(s) for s in self.seed.get("subjects", [])}
        return [
            CheckResult(
                "accepted_reports_completed",
                len(states) == 2 and all(v == "Completed" for v in states.values()),
                detail={"reports": states, "stranded_job": self.seed.get("stranded_job")},
            ),
            CheckResult(
                "accepted_email_delivered_once",
                bool(counts) and all(c == 1 for c in counts.values()),
                detail={"deliveries": counts},
            ),
        ]

    def describe(self) -> str:
        return (
            "Two Prepared Reports and four emails were accepted while the queue was unusable; one report's RQ job "
            "was dropped from the long queue list (job hash still queued), so it must be re-delivered by hand."
        )
