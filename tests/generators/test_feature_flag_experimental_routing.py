import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import yaml
from kubernetes import client

from sregym.generators.fault.inject_app import (
    FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE,
    ApplicationFaultInjector,
)
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HOTEL_RESERVATION_APPLICATION_IMAGE

HOTEL_RESERVATION_DEPLOYMENTS = (
    "frontend/frontend-deployment.yaml",
    "geo/geo-deployment.yaml",
    "profile/profile-deployment.yaml",
    "rate/rate-deployment.yaml",
    "reccomend/recommendation-deployment.yaml",
    "reserve/reservation-deployment.yaml",
    "search/search-deployment.yaml",
    "user/user-deployment.yaml",
)


def test_experimental_routing_image_uses_sregym_latest():
    assert FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE == HOTEL_RESERVATION_APPLICATION_IMAGE

    default = (
        inspect.signature(ApplicationFaultInjector.inject_feature_flag_experimental_routing)
        .parameters["experimental_image"]
        .default
    )
    assert default == FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE


def test_all_first_party_hotel_services_use_the_shared_application_image():
    manifest_root = TARGET_MICROSERVICES / "hotelReservation" / "kubernetes"

    for relative_path in HOTEL_RESERVATION_DEPLOYMENTS:
        manifest = yaml.safe_load((manifest_root / relative_path).read_text())
        containers = manifest["spec"]["template"]["spec"]["containers"]
        assert containers[0]["image"] == HOTEL_RESERVATION_APPLICATION_IMAGE


def test_feature_flag_recovery_removes_env_reference_to_force_a_rollout(monkeypatch):
    container = SimpleNamespace(
        name="hotel-reserv-frontend",
        image=HOTEL_RESERVATION_APPLICATION_IMAGE,
        env=[
            client.V1EnvVar(name="JAEGER_SAMPLE_RATIO", value="1"),
            client.V1EnvVar(name="SEARCH_BACKEND_VERSION", value="true"),
        ],
    )
    deployment = SimpleNamespace(
        spec=SimpleNamespace(
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
            strategy=None,
        )
    )
    injector = ApplicationFaultInjector.__new__(ApplicationFaultInjector)
    injector.namespace = "hotel-reservation"
    injector.kubectl = SimpleNamespace(
        create_or_update_configmap=Mock(),
        get_deployment=Mock(return_value=deployment),
        update_deployment=Mock(),
        exec_command=Mock(),
    )
    monkeypatch.setattr("sregym.generators.fault.inject_app.time.sleep", lambda _: None)

    injector.recover_feature_flag_experimental_routing(
        original_image=HOTEL_RESERVATION_APPLICATION_IMAGE,
    )

    assert [env.name for env in container.env] == ["JAEGER_SAMPLE_RATIO"]
    injector.kubectl.update_deployment.assert_called_once_with(
        "frontend",
        "hotel-reservation",
        deployment,
    )
