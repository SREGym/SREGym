"""Adversarial outcomes for the persistent database recovery task."""

import copy
import unittest

from sregym.conductor.oracles.data_service_state import Event, StateViolation, latest, reconcile, verify_business_state


class BusinessStateTests(unittest.TestCase):
    def setUp(self):
        self.events = {
            "a": Event("a", "account", 10, "first-site"),
            "b": Event("b", "account", 12, "second-site"),
            "c": Event("c", "other", 11, "retained"),
        }
        rows = [event.row() for event in self.events.values()]
        projected = [event.row() for event in latest(self.events).values()]
        self.tables = {
            "journal": rows,
            "effects": rows.copy(),
            "records": projected,
            "search_records": projected.copy(),
            "outbox": [(event_id, 1) for event_id in self.events],
        }

    def test_complete_recovery_passes_independent_of_replay_order(self):
        self.tables["journal"].reverse()
        verify_business_state(self.events, self.tables)

    def test_each_required_table_cannot_be_deleted_or_cleared(self):
        for name in self.tables:
            with self.subTest(table=name):
                tables = copy.deepcopy(self.tables)
                tables[name] = []
                with self.assertRaises(StateViolation):
                    verify_business_state(self.events, tables)

    def test_dropping_an_old_accepted_write_fails_even_if_latest_values_match(self):
        self.tables["journal"] = [row for row in self.tables["journal"] if row[0] != "a"]
        with self.assertRaisesRegex(StateViolation, "accepted_write"):
            verify_business_state(self.events, self.tables)

    def test_forged_done_flags_do_not_replace_business_effects(self):
        self.tables["effects"] = []
        with self.assertRaisesRegex(StateViolation, "business_effect"):
            verify_business_state(self.events, self.tables)

    def test_stale_projection_fails(self):
        self.tables["search_records"][0] = self.events["a"].row()
        with self.assertRaisesRegex(StateViolation, "projection"):
            verify_business_state(self.events, self.tables)

    def test_altered_payload_fails(self):
        self.tables["journal"][0] = ("a", "account", 10, "changed")
        with self.assertRaisesRegex(StateViolation, "accepted_write"):
            verify_business_state(self.events, self.tables)

    def test_duplicate_effect_is_rejected(self):
        self.tables["effects"].append(self.events["a"].row())
        with self.assertRaisesRegex(StateViolation, "duplicate_business_effect"):
            verify_business_state(self.events, self.tables)

    def test_invented_new_revision_cannot_mask_the_accepted_client_value(self):
        extra = Event("fake", "account", 999, "invented")
        self.tables["journal"].append(extra.row())
        self.tables["effects"].append(extra.row())
        self.tables["outbox"].append((extra.event_id, 1))
        self.tables["records"][0] = extra.row()
        self.tables["search_records"][0] = extra.row()
        with self.assertRaisesRegex(StateViolation, "unacknowledged"):
            verify_business_state(self.events, self.tables)

    def test_reconciliation_requires_both_surviving_histories(self):
        rows = [self.events["a"].row(), self.events["b"].row(), self.events["a"].row()]
        merged = reconcile(rows)
        self.assertEqual(set(merged), {"a", "b"})
        self.assertEqual(latest(merged)["account"], self.events["b"])

    def test_conflicting_event_identity_or_revision_fails_closed(self):
        for conflict in [("a", "account", 10, "changed"), ("d", "account", 10, "first-site")]:
            with self.subTest(conflict=conflict), self.assertRaises(StateViolation):
                reconcile([self.events["a"].row(), conflict])


if __name__ == "__main__":
    unittest.main()
