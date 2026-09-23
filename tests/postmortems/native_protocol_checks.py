"""Run inside the app image: python /checks/native_protocol_checks.py."""

import os
import unittest

os.environ.setdefault("ROUTE_SERVICES", "persistence")
os.environ.setdefault("CONSUL_HTTP_ADDR", "http://unused:8500")
os.environ.setdefault("CONSUL_SERVERS", "unused")

from routing import update  # noqa: E402
from subscription_pb2 import Event  # noqa: E402


class NativeProtocolChecks(unittest.TestCase):
    def register(self):
        event = Event()
        row = event.ServiceHealth.CheckServiceNode
        row.Node.Node = "worker-1"
        row.Node.Address = "10.0.0.1"
        row.Service.ID = "allocation-1"
        row.Service.Port = 20000
        row.Checks.add(Status="passing")
        return event

    def test_native_deregister_without_node_address(self):
        table = {}
        event = self.register()
        update(event, table)
        self.assertEqual(list(table.values()), ["http://10.0.0.1:20000"])
        event.ServiceHealth.Op = 1
        event.ServiceHealth.CheckServiceNode.Node.Address = ""
        update(event, table)
        self.assertEqual(table, {})

    def test_critical_check_withdraws_route(self):
        table = {}
        event = self.register()
        update(event, table)
        event.ServiceHealth.CheckServiceNode.Checks[0].Status = "critical"
        update(event, table)
        self.assertEqual(table, {})

    def test_snapshot_reset_discards_old_routes(self):
        table = {}
        update(self.register(), table)
        update(Event(NewSnapshotToFollow=True), table)
        self.assertEqual(table, {})


if __name__ == "__main__":
    unittest.main()
