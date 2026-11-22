"""Registered environment-scaled faults owned by this application."""

from collections.abc import Callable

from sregym.conductor.problems.apps.slack_spine.admission import (
    AdmissionWebhookTLSMismatchSlack,
    CumulativeAdmissionWebhookTimeoutSlack,
    MutatingWebhookResourceLimitsIA,
)
from sregym.conductor.problems.apps.slack_spine.autoscaling import HPAMissingEffectiveCPURequestSlack
from sregym.conductor.problems.apps.slack_spine.cache import RedisAuthDisruptionIA
from sregym.conductor.problems.apps.slack_spine.dns import (
    ServiceDNSResolutionFailureIA,
    StaleCoreDNSConfigSlack,
    StaleHostAliasesSlack,
)
from sregym.conductor.problems.apps.slack_spine.feature_flags import FeatureFlagLatentBugSlack
from sregym.conductor.problems.apps.slack_spine.images import (
    FaultyImageCorrelatedSlack,
    ImageSlowLoadSlack,
    IncorrectImageSlack,
    UpdateIncompatibleCorrelatedSlack,
)
from sregym.conductor.problems.apps.slack_spine.indexing import KafkaPoisonPillHOLBlockIA, KafkaQueueProblemsSlack
from sregym.conductor.problems.apps.slack_spine.networking import (
    CalicoRouteReflectorLabelDriftSlack,
    IngressMisrouteSlack,
    InternalTrafficPolicyLocalIA,
    NetworkPolicyBlockIA,
    PodCIDRExhaustionSlack,
)
from sregym.conductor.problems.apps.slack_spine.readiness import (
    InitContainerDependencyHangSlack,
    LivenessProbeMisconfigurationSlack,
    ReadinessProbeMisconfigurationIA,
)
from sregym.conductor.problems.apps.slack_spine.resources import (
    CpuThrottlingSlack,
    KubeletEvictionThresholdMisconfigSlack,
    WorkloadImbalanceSlack,
)
from sregym.conductor.problems.apps.slack_spine.retry_storm import (
    CapacityDecreaseRPCRetryStormSlack,
    LoadSpikeRPCRetryStormSlack,
    RetryStormCollapseIA,
)
from sregym.conductor.problems.apps.slack_spine.scheduling import (
    DuplicatePVCMountsIA,
    PersistentVolumeAffinityViolationSlack,
    PodAntiAffinityDeadlockSlack,
    ResourceRequestTooLargeSlack,
    TaintNoTolerationSlack,
)
from sregym.conductor.problems.apps.slack_spine.service_configuration import (
    IncorrectPortAssignmentSlack,
    K8STargetPortMisconfigSlack,
    MissingConfigMapSlack,
    MissingEnvVariableSlack,
    MissingServiceSlack,
    ScalePodZeroSlack,
    ServicePortConflictSlack,
    WrongServiceSelectorIA,
)
from sregym.conductor.problems.base import Problem

SOURCE_PORTS: dict[str, tuple[str, Callable[[], Problem]]] = {
    "incorrect_image": ("incorrect_image_slack_spine", IncorrectImageSlack),
    "faulty_image_correlated": ("faulty_image_correlated_slack_spine", FaultyImageCorrelatedSlack),
    "update_incompatible_correlated": ("update_incompatible_correlated_slack_spine", UpdateIncompatibleCorrelatedSlack),
    "incorrect_port_assignment": ("incorrect_port_assignment_slack_spine", IncorrectPortAssignmentSlack),
    "missing_env_variable_astronomy_shop": ("missing_env_variable_slack_spine", MissingEnvVariableSlack),
    "k8s_target_port-misconfig": ("k8s_target_port-misconfig_slack_spine", K8STargetPortMisconfigSlack),
    **{
        f"liveness_probe_misconfiguration_{app}": (
            "liveness_probe_misconfiguration_slack_spine",
            LivenessProbeMisconfigurationSlack,
        )
        for app in ("astronomy_shop", "hotel_reservation", "social_network")
    },
    **{
        f"init_container_dependency_hang_{app}": (
            "init_container_dependency_hang_slack_spine",
            InitContainerDependencyHangSlack,
        )
        for app in ("hotel_reservation", "social_network", "astronomy_shop")
    },
    **{
        f"missing_configmap_{app}": ("missing_configmap_slack_spine", MissingConfigMapSlack)
        for app in ("hotel_reservation", "social_network")
    },
    **{
        f"missing_service_{app}": ("missing_service_slack_spine", MissingServiceSlack)
        for app in ("astronomy_shop", "hotel_reservation", "social_network")
    },
    "scale_pod_zero_social_net": ("scale_pod_zero_slack_spine", ScalePodZeroSlack),
    **{
        f"service_port_conflict_{app}": ("service_port_conflict_slack_spine", ServicePortConflictSlack)
        for app in ("astronomy_shop", "hotel_reservation", "social_network")
    },
    "resource_request_too_large": ("resource_request_too_large_slack_spine", ResourceRequestTooLargeSlack),
    "pod_anti_affinity_deadlock": ("pod_anti_affinity_deadlock_slack_spine", PodAntiAffinityDeadlockSlack),
    "taint_no_toleration_social_network": ("taint_no_toleration_slack_spine", TaintNoTolerationSlack),
    "persistent_volume_affinity_violation": (
        "persistent_volume_affinity_violation_slack_spine",
        PersistentVolumeAffinityViolationSlack,
    ),
    "hpa_missing_effective_cpu_request_hotel_reservation": (
        "hpa_missing_effective_cpu_request_slack_spine",
        HPAMissingEffectiveCPURequestSlack,
    ),
    "cfs_cpu_throttling_hotel_reservation": ("cfs_cpu_throttling_slack_spine", CpuThrottlingSlack),
    "kafka_queue_problems": ("kafka_queue_problems_slack_spine", KafkaQueueProblemsSlack),
    "kubelet_eviction_threshold_misconfig": (
        "kubelet_eviction_threshold_misconfig_slack_spine",
        KubeletEvictionThresholdMisconfigSlack,
    ),
    "workload_imbalance": ("workload_imbalance_slack_spine", WorkloadImbalanceSlack),
    **{
        f"stale_coredns_config_{app}": ("stale_coredns_config_slack_spine", StaleCoreDNSConfigSlack)
        for app in ("astronomy_shop", "social_network")
    },
    "admission_webhook_tls_mismatch_hotel_reservation": (
        "admission_webhook_tls_mismatch_slack_spine",
        AdmissionWebhookTLSMismatchSlack,
    ),
    "cumulative_admission_webhook_timeout_hotel_reservation": (
        "cumulative_admission_webhook_timeout_slack_spine",
        CumulativeAdmissionWebhookTimeoutSlack,
    ),
    "pod_cidr_exhaustion_hotel_reservation": ("pod_cidr_exhaustion_slack_spine", PodCIDRExhaustionSlack),
    "feature_flag_latent_bug_hotel_reservation": ("feature_flag_latent_bug_slack_spine", FeatureFlagLatentBugSlack),
    "astronomy_shop_ad_service_image_slow_load": ("image_slow_load_slack_spine", ImageSlowLoadSlack),
    "calico_route_reflector_label_drift_hotel_reservation": (
        "calico_route_reflector_label_drift_slack_spine",
        CalicoRouteReflectorLabelDriftSlack,
    ),
    "stale_hostaliases_dns_poisoning_astronomy_shop": (
        "stale_hostaliases_dns_poisoning_slack_spine",
        StaleHostAliasesSlack,
    ),
    "ingress_misroute": ("ingress_misroute_slack_spine", IngressMisrouteSlack),
    "capacity_decrease_rpc_retry_storm": (
        "capacity_decrease_rpc_retry_storm_slack_spine",
        CapacityDecreaseRPCRetryStormSlack,
    ),
    "load_spike_rpc_retry_storm": ("load_spike_rpc_retry_storm_slack_spine", LoadSpikeRPCRetryStormSlack),
    "network_policy_block": ("network_policy_block_slack_spine", NetworkPolicyBlockIA),
    "service_dns_resolution_failure_social_network": (
        "service_dns_resolution_failure_slack_spine",
        ServiceDNSResolutionFailureIA,
    ),
    "readiness_probe_misconfiguration_social_network": (
        "readiness_probe_misconfiguration_slack_spine",
        ReadinessProbeMisconfigurationIA,
    ),
    "duplicate_pvc_mounts_social_network": ("duplicate_pvc_mounts_slack_spine", DuplicatePVCMountsIA),
    "wrong_service_selector_social_network": ("wrong_service_selector_slack_spine", WrongServiceSelectorIA),
    "kafka_poison_pill_hol_block": ("kafka_poison_pill_hol_block_slack_spine", KafkaPoisonPillHOLBlockIA),
    "internal_traffic_policy_local_astronomy_shop": (
        "internal_traffic_policy_local_slack_spine",
        InternalTrafficPolicyLocalIA,
    ),
    "mutating_webhook_resource_limits_social_network": (
        "mutating_webhook_resource_limits_slack_spine",
        MutatingWebhookResourceLimitsIA,
    ),
    "search_rate_retry_collapse_hotel_reservation": ("search_rate_retry_collapse_slack_spine", RetryStormCollapseIA),
    "valkey_auth_disruption": ("redis_auth_disruption_slack_spine", RedisAuthDisruptionIA),
}
