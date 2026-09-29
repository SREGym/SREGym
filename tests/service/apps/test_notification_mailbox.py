"""Exercise actual SMTP acceptance, durable effects and read-only evidence."""

import json
import smtplib
import threading
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.server import ThreadingHTTPServer

import pytest

from sregym.service.apps.incident_runtime.mail_sink import Mailbox, SMTPServer, http_handler, smtp_handler


def test_smtp_persistence_duplicates_and_read_only_audit(tmp_path):
    path = tmp_path / "mail.sqlite"
    mailbox = Mailbox(path)
    smtp = SMTPServer(("127.0.0.1", 0), smtp_handler(mailbox))
    audit = ThreadingHTTPServer(("127.0.0.1", 0), http_handler(mailbox))
    threads = [threading.Thread(target=server.serve_forever) for server in (smtp, audit)]
    for thread in threads:
        thread.start()
    try:
        message = EmailMessage()
        message["From"] = "gitlab@sregym.local"
        message["To"] = "user@sregym.local"
        message["Subject"] = "Private issue"
        message["X-GitLab-Issue-IID"] = "12"
        message.set_content("Preserve this text\n.dot-prefixed line\n")
        with smtplib.SMTP(*smtp.server_address) as client:
            client.send_message(message)
            client.send_message(message)
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                client.sendmail("gitlab@sregym.local", ["external@example.org"], message.as_bytes())
        stored = Mailbox(path).messages()
        assert len(stored) == 2
        assert stored[0]["body"].splitlines() == ["Preserve this text", ".dot-prefixed line"]
        assert stored[0]["headers"]["x-gitlab-issue-iid"] == "12"
        url = f"http://127.0.0.1:{audit.server_port}/messages"
        assert json.load(urllib.request.urlopen(url)) == stored
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(url, method="DELETE"))
        assert exc.value.code == 501
        assert mailbox.messages() == stored
    finally:
        for server in (smtp, audit):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
