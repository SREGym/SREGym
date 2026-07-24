import inspect

from sregym.generators.fault.inject_app import (
    FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE,
    ApplicationFaultInjector,
)


def test_experimental_routing_image_uses_sregym_latest():
    assert FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE == "ghcr.io/sregym/hotel-reservation:latest"

    default = (
        inspect.signature(ApplicationFaultInjector.inject_feature_flag_experimental_routing)
        .parameters["experimental_image"]
        .default
    )
    assert default == FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE
