"""Registered environment-scaled faults owned by this application."""

from collections.abc import Callable

from sregym.conductor.problems.apps.frappe.authentication import (
    AuthMissFrappe,
    RevokeAuthFrappe,
    StorageUserUnregisteredFrappe,
)
from sregym.conductor.problems.apps.frappe.configuration import (
    ConfigMapDriftFrappe,
    EdgeRequestFilterIA,
    PSARestrictedBlocksRecreationFrappe,
)
from sregym.conductor.problems.apps.frappe.controllers import (
    CronJobSidecarBlocksCompletionIA,
    FinalizerDeadlockControllerIA,
    RollingUpdateMisconfiguredIA,
    ServiceWrongPodSelectionIA,
)
from sregym.conductor.problems.apps.frappe.networking import (
    EphemeralPortRangeFrappe,
    ExpiredTlsFrappe,
    LoadGeneratorFloodHomepageFrappe,
    NodeConntrackExhaustionFrappe,
)
from sregym.conductor.problems.apps.frappe.resources import DevShmExhaustionFrappe, ValkeyMemoryDisruptionFrappe
from sregym.conductor.problems.apps.frappe.scheduling import IncorrectPortAssignmentIA
from sregym.conductor.problems.apps.frappe.storage import SilentDataCorruptionFrappe
from sregym.conductor.problems.base import Problem

SOURCE_PORTS: dict[str, tuple[str, Callable[[], Problem]]] = {
    "revoke_auth_mongodb-1": ("revoke_auth_frappe", RevokeAuthFrappe),
    "storage_user_unregistered-1": ("storage_user_unregistered_frappe", StorageUserUnregisteredFrappe),
    "auth_miss_mongodb": ("auth_miss_mongodb_frappe", AuthMissFrappe),
    "ephemeral_port_range_hotel_reservation": ("ephemeral_port_range_frappe", EphemeralPortRangeFrappe),
    "configmap_drift_hotel_reservation": ("configmap_drift_frappe", ConfigMapDriftFrappe),
    "valkey_memory_disruption": ("valkey_memory_disruption_frappe", ValkeyMemoryDisruptionFrappe),
    "dev_shm_exhaustion_hotel_reservation": ("dev_shm_exhaustion_frappe", DevShmExhaustionFrappe),
    "node_conntrack_exhaustion_hotel_reservation": ("node_conntrack_exhaustion_frappe", NodeConntrackExhaustionFrappe),
    "silent_data_corruption": ("silent_data_corruption_frappe", SilentDataCorruptionFrappe),
    "loadgenerator_flood_homepage": ("loadgenerator_flood_homepage_frappe", LoadGeneratorFloodHomepageFrappe),
    "expired_tls_hotel_reservation": ("expired_tls_frappe", ExpiredTlsFrappe),
    "psa_restricted_blocks_recreation_hotel_reservation": (
        "psa_restricted_blocks_recreation_frappe",
        PSARestrictedBlocksRecreationFrappe,
    ),
    "cronjob_sidecar_blocks_completion_hotel_reservation": (
        "cronjob_sidecar_blocks_completion_frappe",
        CronJobSidecarBlocksCompletionIA,
    ),
    "edge_request_filter_cpu_saturation": ("edge_request_filter_cpu_saturation_frappe", EdgeRequestFilterIA),
    "finalizer_deadlock_controller_hotel_reservation": (
        "finalizer_deadlock_controller_frappe",
        FinalizerDeadlockControllerIA,
    ),
    "service_wrong_pod_selection_hotel_reservation": ("service_wrong_pod_selection_frappe", ServiceWrongPodSelectionIA),
    "unschedulable_incorrect_port_assignment": (
        "unschedulable_incorrect_port_assignment_frappe",
        IncorrectPortAssignmentIA,
    ),
    "rolling_update_misconfigured_social_network": (
        "rolling_update_misconfigured_frappe",
        RollingUpdateMisconfiguredIA,
    ),
}
