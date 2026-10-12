"""Backend roles of components (database, cache, queue, ...).

jev_diag recognises a role by words in a component's name or image (`derive.backend_role`). A backend named
after the business (`user-store`, image `registry.corp/db:3.2`) is then not recognised: it gets no backend probe,
and errors that name the technology ("postgres: connection refused") are not linked to it. Here a component that
the words do not identify is recognised by its ports instead, but only when every port it serves is a default port
of one and the same backend: application services often expose a metrics or admin port that happens to be some
backend's default (9092, Kafka's, is a common metrics port), so a single matching port proves nothing. A few
message brokers that jev_diag does not list are added.
"""

from __future__ import annotations

import re

from clients.jev_diag import derive

# Default ports of each backend (client, cluster and admin ports it serves itself).
ROLE_PORTS = {
    "redis": {6379, 16379, 26379},
    "mongodb": {27017, 27018, 27019},
    "postgresql": {5432},
    "mysql": {3306, 33060},
    "elasticsearch": {9200, 9300},
    "kafka": {9092, 9093, 9094, 29092},
    "rabbitmq": {5672, 5671, 15672, 25672, 4369},
    "memcached": {11211},
    "zookeeper": {2181, 2888, 3888},
    "nats": {4222, 6222, 8222},
    "pulsar": {6650, 6651},
    "activemq": {61616, 5672, 8161},
}
# Message brokers jev_diag's role list does not name: role -> words in a name or image.
EXTRA_ROLES = {
    "nats": {"nats", "jetstream"},
    "pulsar": {"pulsar"},
    "activemq": {"activemq", "artemis"},
}
MESSAGING_ROLES = {"kafka", "rabbitmq", "nats", "pulsar", "activemq"}
_PORT_RE = re.compile(r"\d+")


def served_ports(comp: dict) -> set[int]:
    """Ports the component's containers declare and its Services expose or target."""
    ports: set[int] = set()
    for c in comp.get("containers") or []:
        ports |= {p for p in c.get("ports") or [] if isinstance(p, int)}
    for svc in comp.get("services") or []:
        for spec in svc.get("ports") or []:
            # "9092->9092/TCP": the Service port and the target port
            ports |= {int(n) for n in _PORT_RE.findall(str(spec).split("/")[0])}
    return ports


def backend_role(comp: dict) -> str | None:
    """The backend role a component plays: from words in its name or image, else from its ports when every port it
    serves is a default port of one and the same backend."""
    role = derive.backend_role(comp)
    if role:
        return role
    tokens = derive._component_tokens(comp)
    for name, words in EXTRA_ROLES.items():
        if tokens & words:
            return name
    ports = served_ports(comp)
    if not ports:
        return None
    roles = [role for role, defaults in ROLE_PORTS.items() if ports <= defaults]
    return roles[0] if len(roles) == 1 else None


def name_aliases(components: dict[str, dict]) -> dict[str, str]:
    """Lowercased words that identify a component in log text (jev_diag's aliases), plus the synonyms of a role
    for the only component that plays it when only its port showed the role."""
    aliases = derive._name_aliases(components)
    players: dict[str, list[str]] = {}
    for cid, comp in components.items():
        if isinstance(comp, dict) and comp.get("name"):
            role = backend_role(comp)
            if role:
                players.setdefault(role, []).append(cid)
    groups = dict(zip(derive.ROLE_NAMES, derive.ROLE_SYNONYMS, strict=True)) | EXTRA_ROLES
    for role, cids in players.items():
        if len(cids) == 1:
            for word in groups.get(role, set()) - {"broker"}:
                aliases.setdefault(word, cids[0])
    return aliases
