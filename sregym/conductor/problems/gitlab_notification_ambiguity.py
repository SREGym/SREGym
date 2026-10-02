"""A failed SMTP attempt may already have caused an irreversible delivery."""

import time

from sregym.conductor.oracles.gitlab_notification_recovery import notification_report
from sregym.conductor.problems.gitlab_notification_recovery import GitLabNotificationRecovery
from sregym.service.apps.gitlab_notification_ambiguity import GitLabNotificationAmbiguity as AmbiguousApplication


class GitLabNotificationAmbiguity(GitLabNotificationRecovery):
    application_class = AmbiguousApplication
    audit_noise_per_batch = 160

    def __init__(self, scale_tier="replicated"):
        super().__init__(scale_tier)
        self.transport_evidence = None
        self.root_cause = self.build_structured_root_cause(
            component="gitlab-ce-db",
            namespace=self.namespace,
            description="Wrong-primary deletion reached every PostgreSQL replica. The usable backup predates "
            "acknowledged issues and queued mail, with sequence gaps permitting internal-ID reuse. SMTP "
            "connections failed both after durable acceptance and before acceptance, leaving ambiguous retries. "
            "Reconcile original data and notification intent using the paginated provider audit without "
            "duplicate or wrong-recipient delivery.",
        )
        self.diagnosis_oracle.expected = self.root_cause

    def prepare_recovery_material(self):
        super().prepare_recovery_material()
        app = self.app
        app.add_audit_noise(self.audit_noise_per_batch, "before-interruption")
        app.provider_fault(3)
        try:
            app.control("start", "sidekiq")
            deadline = time.monotonic() + 240
            while True:
                report = notification_report(self.notifications, app.messages())
                transport = app.transport_report()
                if report["delivered"] == 9 and transport["accepted_without_ack"] == 3:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Ambiguous SMTP fixture did not establish nine accepted messages: {report}, {transport}"
                    )
                time.sleep(2)
        finally:
            # Do not leave a live sender when removing the temporary transport fault.
            app.control("stop", "sidekiq")
            app.provider_fault()
        report = notification_report(self.notifications, app.messages())
        if report["delivered"] != 9 or report["duplicates"] or report["wrong_deliveries"]:
            raise RuntimeError(f"Invalid ambiguous notification fixture: {report}")
        self.transport_evidence = app.transport_report()
        self.retained_incident_jobs = app.incident_jobs()
        expected_jobs = len(self.notifications) - 6
        if len(self.retained_incident_jobs) != expected_jobs:
            raise RuntimeError(
                f"SMTP failures retained {len(self.retained_incident_jobs)} incident jobs; expected {expected_jobs}"
            )
        app.add_audit_noise(self.audit_noise_per_batch, "after-interruption")
        public = app.public_messages()
        if public != app.messages():
            raise RuntimeError("Public provider audit does not expose the full durable evidence")

    def reconcile_notifications(self):
        app = self.app
        app.control("stop", "sidekiq")
        messages = app.public_messages()
        missing = [r for r in self.notifications if notification_report([r], messages)["missing"]]
        app.rebind_notifications(missing)
        app.control("start", "sidekiq")
        deadline = time.monotonic() + 300
        while True:
            report = notification_report(self.notifications, app.public_messages())
            if not report["missing"]:
                return report
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Reference ambiguous-notification recovery did not drain: {report}")
            time.sleep(2)
