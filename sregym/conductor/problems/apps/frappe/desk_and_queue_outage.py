"""Desk and queue outage for Frappe."""

from __future__ import annotations

from sregym.conductor.problems.apps.frappe.base import FrappeProblem
from sregym.conductor.problems.apps.frappe.faults import MariaDBGlobal, RedisQueueFlags
from sregym.service.apps.frappe.operations import HEALTHY_QUEUE_FLAGS, unlimited_or_at_least


class FrappeDeskAndQueueOutage(FrappeProblem):
    """Incident Arena 002: per-account connection cap + min-replicas-to-write on a standalone broker."""

    TASK = "002--frappe--07-desk-and-queue-outage-64526986"
    PROBLEM_ID = "frappe_desk_and_queue_outage"

    def build_legs(self):
        return [
            MariaDBGlobal(
                "max_user_connections",
                "8",
                unlimited_or_at_least(16),
                component="mariadb/mariadb.max-user-connections",
                startup_flags="--max-user-connections=500",
            ),
            RedisQueueFlags(
                ["--min-replicas-to-write", "1", *HEALTHY_QUEUE_FLAGS],
                component="redis-queue/redis-queue.config",
                expect_min_replicas_to_write=0,
            ),
        ]
