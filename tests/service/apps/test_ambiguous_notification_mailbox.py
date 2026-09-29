"""Real SMTP transport failures, atomic acceptance, and paginated evidence."""

import json
import smtplib
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

import pytest

from sregym.service.apps.incident_runtime.ambiguous_mail_sink import (
    AmbiguousMailbox,
    ambiguous_smtp_handler,
    audit_handler,
)
from sregym.service.apps.incident_runtime.mail_sink import SMTPServer

RAW = (
    b"From: gitlab@sregym.local\r\nTo: user@sregym.local\r\nSubject: Receipt\r\n"
    b"X-GitLab-NotificationReason: sregym-ack\r\n\r\nAccepted work\r\n"
)


def test_failed_smtp_attempts_can_have_different_durable_outcomes(tmp_path):
    mailbox = AmbiguousMailbox(tmp_path / "mail.sqlite")
    server = SMTPServer(("127.0.0.1", 0), ambiguous_smtp_handler(mailbox))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        mailbox.set_fault(1)
        for _ in range(2):
            with (
                smtplib.SMTP(*server.server_address, timeout=3) as client,
                pytest.raises(smtplib.SMTPServerDisconnected),
            ):
                client.sendmail("gitlab@sregym.local", ["user@sregym.local"], RAW)
        assert len(AmbiguousMailbox(mailbox.path).messages()) == 1
        mailbox.set_fault()
        with smtplib.SMTP(*server.server_address, timeout=3) as client:
            client.sendmail("gitlab@sregym.local", ["user@sregym.local"], RAW)
        # Retrying a failed send really creates a second accepted message.
        assert len(mailbox.messages()) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_concurrent_transports_cannot_exceed_acceptance_budget(tmp_path):
    mailbox = AmbiguousMailbox(tmp_path / "mail.sqlite")
    mailbox.set_fault(3)
    server = SMTPServer(("127.0.0.1", 0), ambiguous_smtp_handler(mailbox))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def send(_):
        with smtplib.SMTP(*server.server_address, timeout=3) as client, pytest.raises(smtplib.SMTPServerDisconnected):
            client.sendmail("gitlab@sregym.local", ["user@sregym.local"], RAW)

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(send, range(9)))
        assert len(mailbox.messages()) == 3
        with mailbox.connect() as db:
            assert db.execute("SELECT count(*),count(delivery_id) FROM transport_faults").fetchone() == (9, 3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_unrelated_mail_does_not_consume_incident_fault_budget(tmp_path):
    mailbox = AmbiguousMailbox(tmp_path / "mail.sqlite")
    mailbox.set_fault(1)
    server = SMTPServer(("127.0.0.1", 0), ambiguous_smtp_handler(mailbox))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with smtplib.SMTP(*server.server_address, timeout=3) as client:
            client.sendmail("gitlab@sregym.local", ["user@sregym.local"], RAW.replace(b"sregym-ack", b"welcome"))
        with mailbox.connect() as db:
            assert db.execute("SELECT active,remaining FROM fault").fetchone() == (1, 1)
        with smtplib.SMTP(*server.server_address, timeout=3) as client, pytest.raises(smtplib.SMTPServerDisconnected):
            client.sendmail("gitlab@sregym.local", ["user@sregym.local"], RAW)
        assert len(mailbox.messages()) == 2
        with mailbox.connect() as db:
            assert db.execute("SELECT count(*),count(delivery_id) FROM transport_faults").fetchone() == (1, 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_pagination_preserves_snapshot_and_read_only_history(tmp_path):
    mailbox = AmbiguousMailbox(tmp_path / "mail.sqlite")
    for _ in range(43):
        mailbox.accept("gitlab@sregym.local", ["user@sregym.local"], RAW)
    server = ThreadingHTTPServer(("127.0.0.1", 0), audit_handler(mailbox))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        first = json.load(urllib.request.urlopen(base + "/messages"))
        assert len(first["items"]) == 40 and first["total"] == 43
        mailbox.accept("gitlab@sregym.local", ["user@sregym.local"], RAW)
        second = json.load(urllib.request.urlopen(base + first["next"]))
        assert second["next"] is None and second["snapshot"] == first["snapshot"]
        assert [r["id"] for r in first["items"] + second["items"]] == list(range(1, 44))
        assert json.load(urllib.request.urlopen(base + "/messages"))["total"] == 44
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(base + "/messages", method="DELETE"))
        assert exc.value.code == 501
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/messages?after=-1")
        assert exc.value.code == 400 and len(mailbox.messages()) == 44
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
