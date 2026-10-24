# ruff: noqa: I001

from pathlib import Path

import yaml

from sregym.conductor.problems.ad_service_failure import AdServiceFailure
from sregym.conductor.problems.ad_service_high_cpu import AdServiceHighCpu
from sregym.conductor.problems.ad_service_manual_gc import AdServiceManualGc
from sregym.conductor.problems.calico_route_reflector_label_drift import (
    CalicoRouteReflectorLabelDriftHotelReservation,
)
from sregym.conductor.problems.cart_service_failure import CartServiceFailure
from sregym.conductor.problems.cpu_throttling import CpuThrottling
from sregym.conductor.problems.cronjob_sidecar_blocks_completion import (
    CronJobSidecarBlocksCompletionHotelReservation,
)
from sregym.conductor.problems.cumulative_admission_webhook_timeout_hotel_reservation import (
    CumulativeAdmissionWebhookTimeoutHotelReservation,
)
from sregym.conductor.problems.expired_tls_hotel_reservation import ExpiredTlsHotelReservation
from sregym.conductor.problems.gc_capacity_degradation import GCCapacityDegradation
from sregym.conductor.problems.apps.gitlab.notification_delayed_audit import GitLabNotificationDelayedAuditProblem
from sregym.conductor.problems.apps.gitlab.notification_intermittent import GitLabNotificationIntermittentProblem
from sregym.conductor.problems.apps.gitlab.notification_unannounced import (
    GitLabNotificationUnannouncedAmbiguityProblem,
)
from sregym.conductor.problems.environment_ports import PORTED_PROBLEMS
from sregym.conductor.problems.apps.frappe.desk_and_queue_outage import FrappeDeskAndQueueOutage
from sregym.conductor.problems.apps.frappe.writes_and_queue_oom import FrappeWritesAndQueueOOM
from sregym.conductor.problems.apps.saleor.statement_timeout import SaleorCheckoutStatementTimeoutCanary
from sregym.conductor.problems.apps.slack_spine.lock_leaks import SlackDistractorVolumeSeqLock, SlackSeqLockLeak
from sregym.conductor.problems.apps.slack_spine.store_consistency import SlackLoginsUnreadSendsAllSlow
from sregym.conductor.problems.apps.slack_spine.maintenance import SlackMaintenanceCollision
from sregym.conductor.problems.apps.slack_spine.strict_mode import (
    SlackSendsFailComplianceWindow,
    SlackSendsFailStrictMode,
)
from sregym.conductor.problems.apps.slack_spine.sequencer import SlackSplitSequencer
from sregym.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from sregym.conductor.problems.ingress_misroute import IngressMisroute
from sregym.conductor.problems.internal_traffic_policy_local import InternalTrafficPolicyLocalAstronomyShop
from sregym.conductor.problems.kafka_producer_leak import KafkaProducerLeak
from sregym.conductor.problems.kubelet_crash import KubeletCrash
from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import KubeletEvictionThresholdMisconfig
from sregym.conductor.problems.missing_stale_cache_fallback_astronomy_shop import MissingStaleCacheFallbackAstronomyShop
from sregym.conductor.problems.multiple_failures import MultipleIndependentFailures  # noqa: F401
from sregym.conductor.problems.mutating_webhook_resource_limits import MutatingWebhookResourceLimits
from sregym.conductor.problems.nightly_rebalance_oom import NightlyRebalanceOOM
from sregym.conductor.problems.node_clock_drift import NodeClockDriftHotelReservation
from sregym.conductor.problems.priority_preemption_cascade import PriorityPreemptionCascadeHotelReservation
from sregym.conductor.problems.stale_hostaliases_dns_poisoning_astronomy_shop import (
    StaleHostAliasesDNSPoisoningAstronomyShop,
)
from sregym.conductor.problems.apps.stripe.feature_config import StripeFeatureConfigProblem
from sregym.conductor.problems.workload_imbalance import WorkloadImbalance
from sregym.service.kubectl import KubeCtl
from sregym.conductor.problems.slo_problem import slo_problems


# fmt: off
class ProblemRegistry:
    def __init__(self):
        self.PROBLEM_REGISTRY = {
            # ==================== APPLICATION FAULT INJECTOR ====================
            # --- CORRELATED PROBLEMS ---
            # --- REGULAR APPLICATION PROBLEMS ---
            "kafka_producer_leak": KafkaProducerLeak,
            "unschedulable_incorrect_port_assignment": lambda: IncorrectPortAssignment(unschedulable=True),
            # # ==================== VIRTUALIZATION FAULT INJECTOR ====================
            # --- METASTABLE FAILURES ---
            # "cache_flush_capacity_degradation": CacheFlushCapacityDegradation,  # module not yet implemented
            "gc_capacity_degradation": GCCapacityDegradation,
            # --- REGULAR VIRTUALIZATION PROBLEMS ---
            "cfs_cpu_throttling_hotel_reservation": lambda: CpuThrottling(faulty_service="geo"),
            "missing_stale_cache_fallback_astronomy_shop": MissingStaleCacheFallbackAstronomyShop,
            "missing_stale_cache_fallback_long_delay_astronomy_shop": lambda: MissingStaleCacheFallbackAstronomyShop(delay="5s"),
            "nightly_rebalance_oom_hotel_reservation": lambda: NightlyRebalanceOOM(faulty_service="recommendation"),
            "node_clock_drift_hotel_reservation": NodeClockDriftHotelReservation,
            "priority_preemption_cascade_hotel_reservation": PriorityPreemptionCascadeHotelReservation,
            "stale_hostaliases_dns_poisoning_astronomy_shop": StaleHostAliasesDNSPoisoningAstronomyShop,
            # "top_of_rack_router_failure_hotel_reservation": lambda: TopOfRackRouterPartitionHotelReservation(app_name="hotel_reservation", faulty_service="frontend"),
            "gitlab_notification_unannounced_ambiguity_replicated": lambda: GitLabNotificationUnannouncedAmbiguityProblem(
                scale_tier="replicated"
            ),
            "gitlab_notification_intermittent_replicated": lambda: GitLabNotificationIntermittentProblem(scale_tier="replicated"),
            "gitlab_notification_delayed_audit_replicated": lambda: GitLabNotificationDelayedAuditProblem(scale_tier="replicated"),
            "stripe_feature_config_single": lambda: StripeFeatureConfigProblem(scale_tier="single"),
            # ==================== OPENTELEMETRY FAULT INJECTOR ====================
            "astronomy_shop_ad_service_failure": AdServiceFailure,
            "astronomy_shop_ad_service_high_cpu": AdServiceHighCpu,
            "astronomy_shop_ad_service_manual_gc": AdServiceManualGc,
            "astronomy_shop_cart_service_failure": CartServiceFailure,
            # ==================== TRAIN TICKET FAULT INJECTOR ====================
            # ==================== HARDWARE FAULT INJECTOR ====================
            # ----- Hardware-failure compound problems (Tier A) -----
            # ==================== DIRECT K8S API ====================
            "expired_tls_hotel_reservation": ExpiredTlsHotelReservation,
            "ingress_misroute": lambda: IngressMisroute(path="/api", correct_service="frontend-service", wrong_service="recommendation-service"),
            "internal_traffic_policy_local_astronomy_shop": InternalTrafficPolicyLocalAstronomyShop,
            "calico_route_reflector_label_drift_hotel_reservation": CalicoRouteReflectorLabelDriftHotelReservation,

            "mutating_webhook_resource_limits_social_network": MutatingWebhookResourceLimits,
            "cumulative_admission_webhook_timeout_hotel_reservation": CumulativeAdmissionWebhookTimeoutHotelReservation,
            "cronjob_sidecar_blocks_completion_hotel_reservation": CronJobSidecarBlocksCompletionHotelReservation,
            # ==================== MULTIPLE INDEPENDENT FAILURES ====================
            # "port_misconfig_revoke_auth_wrong_svc_selector": \
            #     lambda: MultipleIndependentFailures(problems=[
            #         K8STargetPortMisconfig(faulty_service="user-service"),
            #         MongoDBRevokeAuth(faulty_service="mongodb-geo"),
            #         WrongServiceSelector(app_name="astronomy_shop", faulty_service="frontend")
            # ]),
            # # another concurrent fault problem that deploys all three apps
            # "port_misconfig_misconfig_hotelres_missing_env_var": \
            #     lambda: MultipleIndependentFailures(problems=[
            #         K8STargetPortMisconfig(faulty_service="user-service"),
            #         MisconfigAppHotelRes(),
            #         MissingEnvVariable(app_name="astronomy_shop", faulty_service="frontend")
            # ]),
            # # three concurrent fault problems, each only focuses on one app
            # # astro shop
            # "valkey_memory_disruption_missing_env_var_incorrect_port": \
            #     lambda: MultipleIndependentFailures(problems=[
            #         ValkeyMemoryDisruption(),
            #         MissingEnvVariable(app_name="astronomy_shop", faulty_service="frontend"),
            #         IncorrectPortAssignment()
            #     ]),
            # # hotel res
            # "hotel_res_concurrent_fault": lambda: MultipleIndependentFailures(problems=[
            #     MisconfigAppHotelRes(),
            #     MongoDBRevokeAuth(faulty_service="mongodb-geo"),
            #     MongoDBUserUnregistered(faulty_service="mongodb-rate")
            # ]),
            # # social net
            # "social_net_concurrent_fault": lambda: MultipleIndependentFailures(problems=[
            #     AssignNonExistentNode(),
            #     MongoDBAuthMissing(),
            #     LivenessProbeTooAggressive(app_name="social_network"),
            # ]),
            # ad hoc:
            "kubelet_crash": KubeletCrash,
            "kubelet_eviction_threshold_misconfig": KubeletEvictionThresholdMisconfig,
            "workload_imbalance": WorkloadImbalance,
            # ==================== K8S OPERATOR MISOPERATION ==================
            # ==================== FRAPPE, SALEOR AND SLACK SPINE ====================
            # Ported from Incident Arena (abundant-ai/incident-arena); see docs/incident-arena.md.
            "frappe_desk_and_queue_outage": FrappeDeskAndQueueOutage,
            "frappe_writes_and_queue_oom": FrappeWritesAndQueueOOM,
            "saleor_checkout_statement_timeout_canary": SaleorCheckoutStatementTimeoutCanary,
            "slack_split_sequencer": SlackSplitSequencer,
            "slack_maintenance_collision": SlackMaintenanceCollision,
            "slack_logins_unread_sends_all_slow": SlackLoginsUnreadSendsAllSlow,
            "slack_sends_fail_strict_mode": SlackSendsFailStrictMode,
            "slack_sends_fail_compliance_window": SlackSendsFailComplianceWindow,
            "slack_seq_lock_leak": SlackSeqLockLeak,
            "slack_distractor_volume_seq_lock": SlackDistractorVolumeSeqLock,
            # Existing faults ported to Frappe, Saleor and Slack Spine.
            **PORTED_PROBLEMS,
        }
# fmt: on
        slo_entries = slo_problems()
        duplicates = self.PROBLEM_REGISTRY.keys() & slo_entries.keys()
        if duplicates:
            raise ValueError(f"SLO problem IDs already registered: {', '.join(sorted(duplicates))}")
        self.PROBLEM_REGISTRY.update(slo_entries)
        self.kubectl = KubeCtl()
        self.non_emulated_cluster_problems = ["node_clock_drift_hotel_reservation"]

    def get_problem_instance(self, problem_id: str):
        if problem_id not in self.PROBLEM_REGISTRY:
            raise ValueError(f"Problem ID {problem_id} not found in registry.")

        is_emulated_cluster = self.kubectl.is_emulated_cluster()
        if is_emulated_cluster and problem_id in self.non_emulated_cluster_problems:
            raise RuntimeError(f"Problem ID {problem_id} is not supported in emulated clusters.")

        return self.PROBLEM_REGISTRY.get(problem_id)()

    def get_problem(self, problem_id: str):
        return self.PROBLEM_REGISTRY.get(problem_id)

    def get_problem_ids(self, task_type: str = None, all: bool = False):
        if task_type:
            return [k for k in self.PROBLEM_REGISTRY if task_type in k]
        if all:
            return list(self.PROBLEM_REGISTRY)

        # by default, only run problems defined in tasklist.yml
        file_dir = Path(__file__).parent.parent
        tasklist_path = file_dir / "tasklist.yml"

        if not tasklist_path.exists():
            # if tasklist.yml does not exist, run all the problems
            return list(self.PROBLEM_REGISTRY)

        with open(tasklist_path) as f:
            tasklist = yaml.safe_load(f)
        return list(tasklist["all"]["problems"])


    def get_problem_count(self, task_type: str = None):
        if task_type:
            return len([k for k in self.PROBLEM_REGISTRY if task_type in k])
        return len(self.PROBLEM_REGISTRY)


# Saturated problems. Implementations remain, but these IDs are no longer registered.
# 100% overall pass@3 across evaluated models:
#   assign_to_non_existent_node
#   astronomy_shop_payment_service_unreachable
#   dev_shm_exhaustion_hotel_reservation
#   duplicate_pvc_mounts_astronomy_shop
#   faulty_image_correlated
#   hpa_missing_effective_cpu_request_hotel_reservation
#   incorrect_port_assignment
#   init_container_dependency_hang_astronomy_shop
#   k8s_target_port-misconfig
#   liveness_probe_misconfiguration_astronomy_shop
#   liveness_probe_misconfiguration_hotel_reservation
#   liveness_probe_misconfiguration_social_network
#   missing_configmap_social_network
#   missing_service_astronomy_shop
#   missing_service_social_network
#   pod_anti_affinity_deadlock
#   pvc_claim_mismatch
#   rbac_misconfiguration
#   readiness_probe_misconfiguration_astronomy_shop
#   resource_request_too_large
# 21/21 successes:
#   admission_webhook_outage_hotel_reservation
#   readiness_probe_misconfiguration_social_network
#   wrong_dns_policy_astronomy_shop
#   wrong_service_selector_social_network
# 3/3 with Claude Code on claude-opus-5-5 (SREGym 1.1 problem set, kind, filtered
# internet, after the AlertOracle and permission-mode fixes):
#   admission_webhook_tls_mismatch_hotel_reservation
#   astronomy_shop_ad_service_image_slow_load
#   astronomy_shop_failed_readiness_probe
#   astronomy_shop_payment_service_failure
#   astronomy_shop_product_catalog_service_failure
#   auth_miss_mongodb
#   capacity_decrease_rpc_retry_storm
#   configmap_drift_hotel_reservation
#   duplicate_pvc_mounts_hotel_reservation
#   duplicate_pvc_mounts_social_network
#   edge_request_filter_cpu_saturation
#   env_variable_shadowing_astronomy_shop
#   ephemeral_port_range_hotel_reservation
#   feature_flag_latent_bug_hotel_reservation
#   file_descriptor_exhaustion
#   finalizer_deadlock_controller_hotel_reservation
#   incorrect_image
#   init_container_dependency_hang_hotel_reservation
#   init_container_dependency_hang_social_network
#   integer_overflow_primary_key_astronomy_shop
#   kafka_poison_pill_hol_block
#   kafka_queue_problems
#   latent_sector_error
#   liveness_probe_too_aggressive_astronomy_shop
#   liveness_probe_too_aggressive_hotel_reservation
#   liveness_probe_too_aggressive_social_network
#   load_spike_rpc_retry_storm
#   loadgenerator_flood_homepage
#   misconfig_app_hotel_res
#   missing_configmap_hotel_reservation
#   missing_env_variable_astronomy_shop
#   missing_service_hotel_reservation
#   namespace_memory_limit
#   network_policy_block
#   node_conntrack_exhaustion_hotel_reservation
#   operator_invalid_affinity_toleration
#   operator_non_existent_storage
#   operator_overload_replicas
#   operator_security_context_fault
#   operator_wrong_operator_image
#   operator_wrong_update_strategy_fault
#   persistent_volume_affinity_violation
#   pod_cidr_exhaustion_hotel_reservation
#   postgres_lock_contention_product_catalog
#   psa_restricted_blocks_recreation_hotel_reservation
#   readiness_probe_misconfiguration_hotel_reservation
#   resource_request_too_small
#   revoke_auth_mongodb-1
#   revoke_auth_mongodb-2
#   rolling_update_misconfigured_hotel_reservation
#   rolling_update_misconfigured_social_network
#   scale_pod_zero_social_net
#   search_rate_retry_collapse_hotel_reservation
#   secret_rotation_stale_env_credentials_astronomy_shop
#   service_dns_resolution_failure_astronomy_shop
#   service_dns_resolution_failure_social_network
#   service_port_conflict_astronomy_shop
#   service_port_conflict_hotel_reservation
#   service_port_conflict_social_network
#   service_wrong_pod_selection_hotel_reservation
#   sidecar_port_conflict_astronomy_shop
#   sidecar_port_conflict_hotel_reservation
#   sidecar_port_conflict_social_network
#   silent_data_corruption
#   stale_coredns_config_astronomy_shop
#   stale_coredns_config_social_network
#   storage_user_unregistered-1
#   storage_user_unregistered-2
#   taint_no_toleration_social_network
#   trainticket_f17_nested_sql_select_clause_error
#   trainticket_f22_sql_column_name_mismatch_error
#   update_incompatible_correlated
#   valkey_auth_disruption
#   valkey_memory_disruption
#   wrong_bin_usage
#   wrong_dns_policy_hotel_reservation
#   wrong_dns_policy_social_network
#   wrong_service_selector_astronomy_shop
#   wrong_service_selector_hotel_reservation
