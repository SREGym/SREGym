"""SREGym-Lite problems ported to the larger Incident Arena applications.

Environment scaling: the same 21 Lite faults, injected into Frappe, Saleor or
Slack Spine instead of Hotel Reservation, Social Network or Astronomy Shop.
``LITE_IA_PORTS`` maps each Lite problem id to its port's id and factory;
``LITE_IA_PROBLEMS`` is the registry slice, in Lite order.
"""

from sregym.conductor.problems.lite_ia.config import (
    EdgeRequestFilterIA,
    EnvVariableShadowingIA,
    IncorrectPortAssignmentIA,
)
from sregym.conductor.problems.lite_ia.data import NamespaceMemoryLimitIA, RedisAuthDisruptionIA
from sregym.conductor.problems.lite_ia.k8s import (
    AdmissionWebhookOutageIA,
    CronJobSidecarBlocksCompletionIA,
    DuplicatePVCMountsIA,
    FinalizerDeadlockControllerIA,
    InternalTrafficPolicyLocalIA,
    MutatingWebhookResourceLimitsIA,
    NetworkPolicyBlockIA,
    ReadinessProbeMisconfigurationIA,
    RollingUpdateMisconfiguredIA,
    ServiceDNSResolutionFailureIA,
    ServiceWrongPodSelectionIA,
    WrongDNSPolicyIA,
    WrongServiceSelectorIA,
)
from sregym.conductor.problems.lite_ia.kafka import KafkaPoisonPillHOLBlockIA
from sregym.conductor.problems.lite_ia.retry_storm import RetryStormCollapseIA
from sregym.conductor.problems.lite_ia.secret_rotation import SecretRotationStaleEnvCredentialsIA

# Lite problem id -> (ported problem id, factory)
LITE_IA_PORTS = {
    "cronjob_sidecar_blocks_completion_hotel_reservation": (
        "cronjob_sidecar_blocks_completion_frappe",
        CronJobSidecarBlocksCompletionIA,
    ),
    "edge_request_filter_cpu_saturation": ("edge_request_filter_cpu_saturation_frappe", EdgeRequestFilterIA),
    "network_policy_block": ("network_policy_block_slack_spine", NetworkPolicyBlockIA),
    "env_variable_shadowing_astronomy_shop": ("env_variable_shadowing_saleor", EnvVariableShadowingIA),
    "finalizer_deadlock_controller_hotel_reservation": (
        "finalizer_deadlock_controller_frappe",
        FinalizerDeadlockControllerIA,
    ),
    "service_dns_resolution_failure_social_network": (
        "service_dns_resolution_failure_slack_spine",
        ServiceDNSResolutionFailureIA,
    ),
    "service_wrong_pod_selection_hotel_reservation": ("service_wrong_pod_selection_frappe", ServiceWrongPodSelectionIA),
    "unschedulable_incorrect_port_assignment": (
        "unschedulable_incorrect_port_assignment_frappe",
        IncorrectPortAssignmentIA,
    ),
    "readiness_probe_misconfiguration_social_network": (
        "readiness_probe_misconfiguration_slack_spine",
        ReadinessProbeMisconfigurationIA,
    ),
    "duplicate_pvc_mounts_social_network": ("duplicate_pvc_mounts_slack_spine", DuplicatePVCMountsIA),
    "admission_webhook_outage_hotel_reservation": ("admission_webhook_outage_saleor", AdmissionWebhookOutageIA),
    "wrong_dns_policy_astronomy_shop": ("wrong_dns_policy_saleor", WrongDNSPolicyIA),
    "wrong_service_selector_social_network": ("wrong_service_selector_slack_spine", WrongServiceSelectorIA),
    "rolling_update_misconfigured_social_network": (
        "rolling_update_misconfigured_frappe",
        RollingUpdateMisconfiguredIA,
    ),
    "kafka_poison_pill_hol_block": ("kafka_poison_pill_hol_block_slack_spine", KafkaPoisonPillHOLBlockIA),
    "internal_traffic_policy_local_astronomy_shop": (
        "internal_traffic_policy_local_slack_spine",
        InternalTrafficPolicyLocalIA,
    ),
    "mutating_webhook_resource_limits_social_network": (
        "mutating_webhook_resource_limits_slack_spine",
        MutatingWebhookResourceLimitsIA,
    ),
    "namespace_memory_limit": ("namespace_memory_limit_saleor", NamespaceMemoryLimitIA),
    "secret_rotation_stale_env_credentials_astronomy_shop": (
        "secret_rotation_stale_env_credentials_saleor",
        SecretRotationStaleEnvCredentialsIA,
    ),
    "search_rate_retry_collapse_hotel_reservation": (
        "search_rate_retry_collapse_slack_spine",
        RetryStormCollapseIA,
    ),
    "valkey_auth_disruption": ("redis_auth_disruption_slack_spine", RedisAuthDisruptionIA),
}

LITE_IA_PROBLEMS = {problem_id: factory for problem_id, factory in LITE_IA_PORTS.values()}

__all__ = ["LITE_IA_PORTS", "LITE_IA_PROBLEMS"]
