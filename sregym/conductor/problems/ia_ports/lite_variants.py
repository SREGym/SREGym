"""Other-app variants of the SREGym-Lite families, served by the Lite ports."""

from __future__ import annotations

from sregym.conductor.problems.lite_ia import LITE_IA_PORTS

# Variant id -> the Lite problem whose port already covers the same fault.
_VARIANT_OF = {
    "duplicate_pvc_mounts_astronomy_shop": "duplicate_pvc_mounts_social_network",
    "duplicate_pvc_mounts_hotel_reservation": "duplicate_pvc_mounts_social_network",
    "readiness_probe_misconfiguration_astronomy_shop": "readiness_probe_misconfiguration_social_network",
    "readiness_probe_misconfiguration_hotel_reservation": "readiness_probe_misconfiguration_social_network",
    "rolling_update_misconfigured_hotel_reservation": "rolling_update_misconfigured_social_network",
    "service_dns_resolution_failure_astronomy_shop": "service_dns_resolution_failure_social_network",
    "wrong_dns_policy_hotel_reservation": "wrong_dns_policy_astronomy_shop",
    "wrong_dns_policy_social_network": "wrong_dns_policy_astronomy_shop",
    "wrong_service_selector_astronomy_shop": "wrong_service_selector_social_network",
    "wrong_service_selector_hotel_reservation": "wrong_service_selector_social_network",
}

PORTS: dict[str, tuple[str, type]] = {variant: LITE_IA_PORTS[lite] for variant, lite in _VARIANT_OF.items()}
