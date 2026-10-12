"""Frappe write-permission and queue-capacity outage."""

from __future__ import annotations

from sregym.conductor.problems.apps.frappe.backlog import StrandedBacklogSeed
from sregym.conductor.problems.apps.frappe.base import FrappeProblem
from sregym.conductor.problems.apps.frappe.faults import MariaDBGlobal, RedisQueueFlags, SiteGrantRevocation
from sregym.service.apps.frappe.operations import is_off


class FrappeWritesAndQueueOOM(FrappeProblem):
    """Incident Arena 005: read-only MariaDB masking revoked grants + 4mb queue that cannot dequeue."""

    TASK = "005--frappe--07-writes-and-queue-oom-f1db8f42"
    PROBLEM_ID = "frappe_writes_and_queue_oom"

    QUEUE_BASE = [
        "--appendonly",
        "yes",
        "--appendfsync",
        "everysec",
        "--user",
        "default",
        "on",
        "nopass",
        "~*",
        "&*",
        "+@all",
        "-flushall",
        "-flushdb",
    ]

    def deploy_values(self):
        values = self.task.task_values
        return {
            "images": values["images"],
            # A deliberately tiny, evicting cache tier: a decoy, not a cause.
            "erpnext": {
                "redis-cache": {"master": {"extraFlags": ["--maxmemory", "4mb", "--maxmemory-policy", "allkeys-lru"]}}
            },
        }

    def build_legs(self):
        healthy = ["--maxmemory", "64mb", "--maxmemory-policy", "noeviction", *self.QUEUE_BASE]
        faulty = [
            "--maxmemory",
            "4mb",
            "--maxmemory-policy",
            "noeviction",
            *self.QUEUE_BASE,
            "-blpop",
            "-blmove",
            "-brpop",
        ]
        return [
            RedisQueueFlags(
                faulty,
                component="redis-queue/redis-queue.config",
                healthy_flags=healthy,
                expected_acl_rules=["+@all", "-flushall", "-flushdb"],
                expect_appendfsync=("everysec", "always"),
            ),
            StrandedBacklogSeed(),
            MariaDBGlobal("read_only", "1", is_off, component="mariadb/mariadb.read-only"),
            SiteGrantRevocation("INSERT", "UPDATE"),
        ]

    def recovery_order(self):
        # Unlock MariaDB, repair the broker, then reconcile the stranded backlog.
        queue, seed, read_only, grants = self.legs
        return [grants, read_only, queue, seed]
