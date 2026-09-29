"""Real SMTP effects during an ongoing, persistent transport impairment."""

import smtplib
import tempfile
import threading
import unittest
from contextlib import suppress
from pathlib import Path

from sregym.service.apps.incident_runtime.ambiguous_mail_sink import ambiguous_smtp_handler
from sregym.service.apps.incident_runtime.intermittent_mail_sink import IntermittentMailbox
from sregym.service.apps.incident_runtime.mail_sink import SMTPServer

RAW = (
    b"From: gitlab@sregym.local\r\nTo: user@sregym.local\r\nSubject: Receipt\r\n"
    b"X-GitLab-NotificationReason: sregym-ack\r\n\r\nAccepted work\r\n"
)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mailbox = IntermittentMailbox(Path(self.directory.name) / "mail.sqlite")
        self.server = SMTPServer(("127.0.0.1", 0), ambiguous_smtp_handler(self.mailbox))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def send(self, raw=RAW):
        with smtplib.SMTP(*self.server.server_address, timeout=3) as client:
            client.sendmail("gitlab@sregym.local", ["user@sregym.local"], raw)

    def test_naive_retry_duplicates_an_accepted_message(self):
        self.mailbox.set_ongoing_fault(True)
        for _ in range(2):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                self.send()
        self.assertEqual(len(self.mailbox.messages()), 1)
        self.send()
        self.assertEqual(len(self.mailbox.messages()), 2)
        self.assertEqual({m["subject"] for m in self.mailbox.messages()}, {"Receipt"})

    def test_reconcile_after_each_transport_result_preserves_exactly_once(self):
        self.mailbox.set_ongoing_fault(True)
        for number in range(12):
            subject = f"Intent {number}"
            raw = RAW.replace(b"Receipt", subject.encode())
            for _ in range(4):
                # This models the repair algorithm's public audit read; the
                # provider's HTTP pagination is tested by the shared module.
                if any(m["subject"] == subject for m in self.mailbox.page()["items"]):
                    break
                with suppress(smtplib.SMTPServerDisconnected):
                    self.send(raw)
            else:
                self.fail("A bounded repair did not establish accepted delivery")
        messages = self.mailbox.messages()
        self.assertEqual(len(messages), 12)
        self.assertEqual(len({m["subject"] for m in messages}), 12)

    def test_counter_and_fault_survive_provider_restart_and_ignore_normal_mail(self):
        self.mailbox.set_ongoing_fault(True)
        self.send(RAW.replace(b"sregym-ack", b"sregym-probe-new"))
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.stop_server()
        self.mailbox = IntermittentMailbox(self.mailbox.path)
        self.server = SMTPServer(("127.0.0.1", 0), ambiguous_smtp_handler(self.mailbox))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.assertEqual(len(self.mailbox.messages()), 2)
        self.send()
        with self.mailbox.connect() as db:
            self.assertEqual(db.execute("SELECT active,attempts FROM ongoing_fault").fetchone(), (1, 3))
            self.assertEqual(db.execute("SELECT count(*),count(delivery_id) FROM transport_faults").fetchone(), (2, 1))

    def test_disabled_mode_preserves_the_original_setup_fault(self):
        self.mailbox.set_fault(1)
        for _ in range(2):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                self.send()
        self.assertEqual(len(self.mailbox.messages()), 1)
        self.mailbox.set_fault()
        self.send()
        self.assertEqual(len(self.mailbox.messages()), 2)


if __name__ == "__main__":
    unittest.main()
