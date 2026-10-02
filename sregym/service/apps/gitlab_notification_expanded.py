"""More tenant data and recovery work on the same replicated GitLab topology."""

import json

from sregym.service.apps.gitlab_notification_delayed_audit import GitLabNotificationDelayedAudit


class GitLabNotificationExpanded(GitLabNotificationDelayedAudit):
    reconciliation_batches = 8

    @property
    def recovery_counts(self):
        return (20, 300, 120)

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += " Expanded workload: 20 tenant projects and 420 issues across three PostgreSQL members."
        return result

    def deploy(self):
        super().deploy()
        tenants, historical, acknowledged = self.recovery_counts
        self.archive_write(
            "/recovery/workload-scale.json",
            json.dumps(
                {
                    "workload_tier": "expanded",
                    "physical_tier": self.scale_tier,
                    "incident_tenant_projects": tenants,
                    "historical_issues": historical,
                    "acknowledged_issues": acknowledged,
                    "notification_intents": acknowledged,
                },
                indent=2,
            )
            + "\n",
        )
