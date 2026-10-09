"""Judge-side invariants for the data service; never deployed into the target."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    event_id: str
    record_key: str
    revision: int
    value: str

    def row(self) -> tuple:
        return self.event_id, self.record_key, self.revision, self.value


class StateViolation(ValueError):
    """An observed business outcome violates the service contract."""


def reconcile(rows: list[tuple]) -> dict[str, Event]:
    """Merge surviving journals without silently choosing between conflicting identities."""
    merged = {}
    versions = {}
    for row in rows:
        event = Event(*row)
        if event.event_id in merged and merged[event.event_id] != event:
            raise StateViolation("conflicting_event_identity")
        version = (event.record_key, event.revision)
        if version in versions and versions[version] != event:
            raise StateViolation("conflicting_record_revision")
        merged[event.event_id] = event
        versions[version] = event
    return merged


def latest(events: dict[str, Event]) -> dict[str, Event]:
    """The highest client revision wins, independently of arrival or replay order."""
    result = {}
    for event in events.values():
        if event.record_key not in result or event.revision > result[event.record_key].revision:
            result[event.record_key] = event
    return result


def verify_business_state(expected: dict[str, Event], tables: dict[str, list[tuple]]) -> None:
    """Check actual rows, including effects, rather than queue counters or health claims."""
    journal = reconcile(tables["journal"])
    if len(journal) != len(tables["journal"]):
        raise StateViolation("duplicate_journal_identity")
    for event_id, event in expected.items():
        if journal.get(event_id) != event:
            raise StateViolation("accepted_write_missing_or_changed")

    # A forged done flag, a deleted queue, or a partially replayed history fails.
    effects = reconcile(tables["effects"])
    outbox = dict(tables["outbox"])
    if len(effects) != len(tables["effects"]) or len(outbox) != len(tables["outbox"]):
        raise StateViolation("duplicate_business_effect")
    for event_id, event in journal.items():
        if effects.get(event_id) != event or outbox.get(event_id) != 1:
            raise StateViolation("business_effect_missing_or_changed")
    if effects.keys() != journal.keys() or outbox.keys() != journal.keys():
        raise StateViolation("orphaned_business_state")

    projections = latest(journal)
    for key, event in latest(expected).items():
        if projections.get(key) != event:
            raise StateViolation("unacknowledged_write_overrides_client_state")
    for table in ("records", "search_records"):
        observed = {row[1]: Event(*row) for row in tables[table]}
        if len(observed) != len(tables[table]) or observed != projections:
            raise StateViolation("projection_incorrect")
