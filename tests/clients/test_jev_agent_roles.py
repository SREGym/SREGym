"""Backend roles by name, image, or the well-known port a component serves."""

from __future__ import annotations

from clients.jev_agent import roles


def comp(name, image="registry.corp/app:1", ports=(), svc_ports=()):
    return {
        "name": name,
        "containers": [{"name": name, "image": image, "ports": list(ports)}],
        "services": [{"name": name, "ports": list(svc_ports)}] if svc_ports else [],
    }


def test_a_backend_named_after_the_business_is_recognised_by_its_port():
    assert roles.backend_role(comp("user-store", "registry.corp/db:3.2", svc_ports=["5432->5432/TCP"])) == "postgresql"
    assert roles.backend_role(comp("sessions", ports=[6379])) == "redis"
    assert roles.backend_role(comp("orders-bus", ports=[4222])) == "nats"


def test_name_and_image_win_and_ambiguous_ports_give_no_role():
    assert roles.backend_role(comp("cart-valkey", ports=[5432])) == "redis"  # the name says it
    assert roles.backend_role(comp("events", "nats:2.10")) == "nats"
    assert roles.backend_role(comp("gateway", ports=[5432, 6379])) is None  # two backends' ports: no guess
    assert roles.backend_role(comp("web", ports=[8080])) is None


def test_errors_naming_the_technology_point_at_the_only_backend_of_that_role():
    comps = {
        "deployment/user-store": comp("user-store", svc_ports=["5432->5432/TCP"]),
        "deployment/web": comp("web", ports=[8080]),
    }
    aliases = roles.name_aliases(comps)
    assert aliases["postgres"] == "deployment/user-store" and aliases["web"] == "deployment/web"


def test_an_application_port_next_to_a_backend_default_port_proves_nothing():
    # hotel-reservation's search serves its API on 8082 and metrics on 9092, Kafka's default port
    assert (
        roles.backend_role(comp("search", ports=[8082, 9092], svc_ports=["8082->8082/TCP", "9092->9092/TCP"])) is None
    )
    assert roles.backend_role(comp("events-store", ports=[9092, 9093])) == "kafka"
