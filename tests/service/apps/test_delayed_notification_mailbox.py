"""Exercise ambiguous SMTP acceptance against a lagging public HTTP audit."""

import json
import smtplib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import suppress
from http.server import ThreadingHTTPServer
from pathlib import Path

from sregym.service.apps.incident_runtime.ambiguous_mail_sink import ambiguous_smtp_handler
from sregym.service.apps.incident_runtime.delayed_mail_sink import DelayedAuditMailbox, delayed_audit_handler
from sregym.service.apps.incident_runtime.mail_sink import SMTPServer

RAW = (
    b"From: gitlab@sregym.local\r\nTo: user@sregym.local\r\nSubject: Receipt\r\n"
    b"X-GitLab-NotificationReason: sregym-ack\r\n\r\nAccepted work\r\n"
)


class DelayedAuditTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mailbox = DelayedAuditMailbox(Path(self.directory.name) / "mail.sqlite", delay=0.15)
        self.start_servers()
        self.addCleanup(self.stop_servers)

    def start_servers(self):
        self.smtp = SMTPServer(("127.0.0.1", 0), ambiguous_smtp_handler(self.mailbox))
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), delayed_audit_handler(self.mailbox))
        self.threads = [threading.Thread(target=s.serve_forever) for s in (self.smtp, self.http)]
        for thread in self.threads:
            thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def stop_servers(self):
        for server in (self.smtp, self.http):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()

    def send(self, raw=RAW):
        with smtplib.SMTP(*self.smtp.server_address, timeout=3) as client:
            client.sendmail("gitlab@sregym.local", ["user@sregym.local"], raw)

    def get(self, path="/messages"):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return json.load(response)

    def publish(self):
        with self.mailbox.connect() as db:
            clock = db.execute("SELECT max(visible_at) FROM audit_visibility").fetchone()[0] + 1
        self.mailbox.clock = lambda: clock

    def test_accepted_without_ack_is_initially_absent_and_survives_restart(self):
        self.mailbox.set_ongoing_fault(True)
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.assertEqual(len(self.mailbox.messages()), 1)
        first = self.get()
        self.assertEqual(first["items"], [])
        with self.mailbox.connect() as db:
            before = db.execute("SELECT * FROM audit_visibility").fetchall()
        self.stop_servers()
        self.mailbox = DelayedAuditMailbox(self.mailbox.path)
        self.start_servers()
        self.assertEqual(self.mailbox.delay, 0.15)
        with self.mailbox.connect() as db:
            self.assertEqual(db.execute("SELECT * FROM audit_visibility").fetchall(), before)
            self.assertEqual(db.execute("SELECT active,attempts FROM ongoing_fault").fetchone(), (1, 1))
        self.publish()
        self.assertEqual(len(self.get()["items"]), 1)

    def test_naive_retry_from_incomplete_snapshot_duplicates(self):
        self.mailbox.set_ongoing_fault(True)
        for _ in range(2):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                self.send()
            self.assertEqual(self.get()["items"], [])
        self.send()
        self.publish()
        self.assertEqual(len(self.get()["items"]), 2)

    def test_watermark_allows_safe_recovery_of_both_transport_outcomes(self):
        self.mailbox.set_ongoing_fault(True)
        for number in range(9):
            subject = f"Intent {number}"
            raw = RAW.replace(b"Receipt", subject.encode())
            for _ in range(4):
                with suppress(smtplib.SMTPServerDisconnected):
                    self.send(raw)
                page = self.get()
                barrier = page["observed_at"]
                deadline = time.monotonic() + 5
                while page["complete_through"] < barrier:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                    page = self.get()
                if any(m["subject"] == subject for m in page["items"]):
                    break
            else:
                self.fail("No accepted delivery after bounded reconciliation")
        messages = self.mailbox.messages()
        self.assertEqual(len(messages), 9)
        self.assertEqual(len({m["subject"] for m in messages}), 9)
        with self.mailbox.connect() as db:
            accepted, rejected = db.execute(
                "SELECT SUM(delivery_id IS NOT NULL),SUM(delivery_id IS NULL) FROM transport_faults"
            ).fetchone()
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 0)

    def test_pagination_keeps_publication_snapshot_stable(self):
        normal = RAW.replace(b"sregym-ack", b"scheduled-digest")
        for _ in range(41):
            self.send(normal)
        self.mailbox.set_ongoing_fault(True)
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.send(normal)
        first = self.get()
        self.assertEqual(first["total"], 42)
        self.publish()
        second = self.get(first["next"])
        self.assertEqual(second["snapshot"], first["snapshot"])
        self.assertEqual(second["observed_at"], first["observed_at"])
        self.assertEqual(second["total"], 42)
        self.assertEqual(len(first["items"] + second["items"]), 42)
        self.assertIsNone(second["next"])
        self.assertEqual(self.get()["total"], 43)

    def test_batch_reconciliation_handles_lag_without_retrying_accepted_mail(self):
        self.mailbox.set_ongoing_fault(True)
        expected = {f"Intent {n}" for n in range(12)}
        remaining = expected.copy()
        for _ in range(4):
            for subject in sorted(remaining):
                with suppress(smtplib.SMTPServerDisconnected):
                    self.send(RAW.replace(b"Receipt", subject.encode()))
            page = self.get()
            barrier = page["observed_at"]
            deadline = time.monotonic() + 5
            while page["complete_through"] < barrier:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                page = self.get()
            remaining = expected - {m["subject"] for m in page["items"]}
            if not remaining:
                break
        self.assertFalse(remaining)
        messages = self.mailbox.messages()
        self.assertEqual(len(messages), len(expected))
        self.assertEqual({m["subject"] for m in messages}, expected)

    def test_normal_mail_and_initial_fixture_remain_immediately_visible(self):
        self.mailbox.set_fault(1)
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.assertEqual(self.get()["total"], 1)
        self.mailbox.set_fault()
        self.mailbox.set_ongoing_fault(True)
        self.send(RAW.replace(b"sregym-ack", b"sregym-probe-new"))
        self.assertEqual(self.get()["total"], 2)
        with self.mailbox.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM ongoing_fault").fetchone()[0], 0)

    def test_invalid_cursor_cannot_reveal_unpublished_mail(self):
        for query in ("observed_at=inf", "observed_at=nan", "observed_at=9999999999", "after=-1", "through=-1"):
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as caught:
                self.get("/messages?" + query)
            self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
