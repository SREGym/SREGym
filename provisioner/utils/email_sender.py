import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv(override=True)


class EmailSender:
    # SMTP Configuration
    SMTP_SERVER = os.getenv("SMTP_SERVER", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", ""))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")  # For Gmail, this should be an App Password
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    # Email Configuration
    DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", SMTP_USERNAME)
    DEFAULT_REPLY_TO = os.getenv("DEFAULT_REPLY_TO", "")
    EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "30"))  # seconds

    @staticmethod
    def is_gmail_app_password(password: str) -> bool:
        return bool(re.match(r"^[a-zA-Z0-9]{16}$", password))

    def __init__(
        self,
        smtp_server: str = None,
        smtp_port: int = None,
        username: str = None,
        password: str = None,
        use_tls: bool = None,
    ):
        self.smtp_server = smtp_server or self.SMTP_SERVER
        self.smtp_port = smtp_port or self.SMTP_PORT
        self.username = username or self.SMTP_USERNAME
        self.password = password or self.SMTP_PASSWORD
        self.use_tls = use_tls if use_tls is not None else self.SMTP_USE_TLS

        # Validate Gmail configuration
        if "gmail.com" in self.smtp_server.lower():
            if not self.is_gmail_app_password(self.password):
                raise ValueError("For Gmail, you must use an App Password.")

    def is_email_set(self) -> bool:
        return (
            self.username is not None
            and self.password is not None
            and self.smtp_server is not None
            and self.smtp_port is not None
        )

    def send_email(
        self,
        to_addresses: List[str],
        subject: str,
        body: str,
        cc_addresses: Optional[List[str]] = None,
        bcc_addresses: Optional[List[str]] = None,
        is_html: bool = False,
    ) -> bool:
        try:
            # Create message
            msg = MIMEMultipart()
            msg["From"] = self.username
            msg["To"] = ", ".join(to_addresses)
            msg["Subject"] = subject

            if cc_addresses:
                msg["Cc"] = ", ".join(cc_addresses)

            # Attach body
            content_type = "html" if is_html else "plain"
            msg.attach(MIMEText(body, content_type))

            # Combine all recipients
            all_recipients = to_addresses.copy()
            if cc_addresses:
                all_recipients.extend(cc_addresses)
            if bcc_addresses:
                all_recipients.extend(bcc_addresses)

            # Connect to SMTP server and send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg, self.username, all_recipients)

            return True

        except Exception as e:
            return False

    def send_html_email(
        self,
        to_addresses: List[str],
        subject: str,
        html_body: str,
        cc_addresses: Optional[List[str]] = None,
        bcc_addresses: Optional[List[str]] = None,
    ) -> bool:
        return self.send_email(
            to_addresses=to_addresses,
            subject=subject,
            body=html_body,
            cc_addresses=cc_addresses,
            bcc_addresses=bcc_addresses,
            is_html=True,
        )

    def send_inactive_cluster_deletion_notice(
        self,
        to_addresses: List[str],
        cluster_name: str,
        last_activity: str,
    ) -> bool:
        """
        Send notification about an inactive cluster scheduled for deletion.
        """
        subject = f"Cluster '{cluster_name}' Scheduled for Deletion"

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <p>Your cluster <strong>{cluster_name}</strong> has been inactive for quite a while and is being deleted.</strong>.</p>
            <p>Last activity: {last_activity}</p>
        </body>
        </html>
        """

        return self.send_html_email(to_addresses=to_addresses, subject=subject, html_body=html_body)

    def send_cluster_extension_failure_notice(
        self,
        to_addresses: List[str],
        cluster_name: str,
        error_message: str,
        current_expiry: str,
    ) -> bool:
        """
        Send notification about a failed cluster extension attempt.
        """
        subject = f"Failed to Extend Cluster '{cluster_name}'"

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <p>Failed to extend cluster <strong>{cluster_name}</strong>.</p>
            <p>Error: {error_message}</p>
            <p>Current expiry: {current_expiry}</p>
        </body>
        </html>
        """

        return self.send_html_email(to_addresses=to_addresses, subject=subject, html_body=html_body)
