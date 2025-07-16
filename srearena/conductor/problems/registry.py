from srearena.conductor.problems.ad_service_failure import AdServiceFailure
from srearena.conductor.problems.ad_service_high_cpu import AdServiceHighCpu
from srearena.conductor.problems.ad_service_manual_gc import AdServiceManualGc
from srearena.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from srearena.conductor.problems.auth_miss_mongodb import MongoDBAuthMissing
from srearena.conductor.problems.cart_service_failure import CartServiceFailure
from srearena.conductor.problems.configmap_drift import ConfigMapDrift
from srearena.conductor.problems.container_kill import ChaosMeshContainerKill
from srearena.conductor.problems.duplicate_pvc_mounts import DuplicatePVCMounts
from srearena.conductor.problems.env_variable_leak import EnvVariableLeak
from srearena.conductor.problems.env_variable_shadowing import EnvVariableShadowing
from srearena.conductor.problems.image_slow_load import ImageSlowLoad
from srearena.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from srearena.conductor.problems.liveness_probe_misconfiguration import LivenessProbeMisconfiguration
from srearena.conductor.problems.liveness_probe_too_aggressive import LivenessProbeTooAggressive
from srearena.conductor.problems.loadgenerator_flood_homepage import LoadGeneratorFloodHomepage
from srearena.conductor.problems.misconfig_app import MisconfigAppHotelRes
from srearena.conductor.problems.missing_service import MissingService
from srearena.conductor.problems.network_delay import ChaosMeshNetworkDelay
from srearena.conductor.problems.network_loss import ChaosMeshNetworkLoss
from srearena.conductor.problems.payment_service_failure import PaymentServiceFailure
from srearena.conductor.problems.payment_service_unreachable import PaymentServiceUnreachable
from srearena.conductor.problems.pod_failure import ChaosMeshPodFailure
from srearena.conductor.problems.pod_kill import ChaosMeshPodKill
from srearena.conductor.problems.product_catalog_failure import ProductCatalogServiceFailure
from srearena.conductor.problems.readiness_probe_misconfiguration import ReadinessProbeMisconfiguration
from srearena.conductor.problems.recommendation_service_cache_failure import RecommendationServiceCacheFailure
from srearena.conductor.problems.redeploy_without_pv import RedeployWithoutPV
from srearena.conductor.problems.resource_request import ResourceRequestTooLarge, ResourceRequestTooSmall
from srearena.conductor.problems.revoke_auth import MongoDBRevokeAuth
from srearena.conductor.problems.scale_pod import ScalePodSocialNet
from srearena.conductor.problems.service_dns_resolution_failure import ServiceDNSResolutionFailure
from srearena.conductor.problems.sidecar_port_conflict import SidecarPortConflict
from srearena.conductor.problems.stale_coredns_config import StaleCoreDNSConfig
from srearena.conductor.problems.storage_user_unregistered import MongoDBUserUnregistered
from srearena.conductor.problems.taint_no_toleration import TaintNoToleration
from srearena.conductor.problems.target_port import K8STargetPortMisconfig
from srearena.conductor.problems.wrong_bin_usage import WrongBinUsage
from srearena.conductor.problems.wrong_dns_policy import WrongDNSPolicy
from srearena.conductor.problems.wrong_service_selector import WrongServiceSelector
from srearena.conductor.problems.network_policy_block import NetworkPolicyBlock
from srearena.conductor.problems.taint_no_toleration import TaintNoToleration
from srearena.conductor.problems.rolling_update_misconfigured import RollingUpdateMisconfigured
from srearena.conductor.problems.ingress_misroute import IngressMisroute



class ProblemRegistry:
    def __init__(self):
        self.PROBLEM_REGISTRY = {
            "k8s_target_port-misconfig": lambda: K8STargetPortMisconfig(faulty_service="user-service"),
            "auth_miss_mongodb": MongoDBAuthMissing,
            "revoke_auth_mongodb-1": lambda: MongoDBRevokeAuth(faulty_service="mongodb-geo"),
            "revoke_auth_mongodb-2": lambda: MongoDBRevokeAuth(faulty_service="mongodb-rate"),
            "storage_user_unregistered-1": lambda: MongoDBUserUnregistered(faulty_service="mongodb-geo"),
            "storage_user_unregistered-2": lambda: MongoDBUserUnregistered(faulty_service="mongodb-rate"),
            "misconfig_app_hotel_res": MisconfigAppHotelRes,
            "scale_pod_zero_social_net": ScalePodSocialNet,
            "assign_to_non_existent_node": AssignNonExistentNode,
            "chaos_mesh_container_kill": ChaosMeshContainerKill,
            "chaos_mesh_pod_failure": ChaosMeshPodFailure,
            "chaos_mesh_pod_kill": ChaosMeshPodKill,
            "chaos_mesh_network_loss": ChaosMeshNetworkLoss,
            "chaos_mesh_network_delay": ChaosMeshNetworkDelay,
            "astronomy_shop_ad_service_failure": AdServiceFailure,
            "astronomy_shop_ad_service_high_cpu": AdServiceHighCpu,
            "astronomy_shop_ad_service_manual_gc": AdServiceManualGc,
            "astronomy_shop_kafka_queue_problems": KafkaQueueProblems,
            "astronomy_shop_cart_service_failure": CartServiceFailure,
            "astronomy_shop_image_slow_load": ImageSlowLoad,
            "astronomy_shop_loadgenerator_flood_homepage": LoadGeneratorFloodHomepage,
            "astronomy_shop_payment_service_failure": PaymentServiceFailure,
            "astronomy_shop_payment_service_unreachable": PaymentServiceUnreachable,
            "astronomy_shop_product_catalog_service_failure": ProductCatalogServiceFailure,
            "astronomy_shop_recommendation_service_cache_failure": RecommendationServiceCacheFailure,
            "redeploy_without_PV": RedeployWithoutPV,
            "wrong_bin_usage": WrongBinUsage,
            "taint_no_toleration_social_network": lambda: TaintNoToleration(),
            "missing_service_hotel_reservation": lambda: MissingService(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ),
            "missing_service_social_network": lambda: MissingService(
                app_name="social_network", faulty_service="user-service"
            ),
            "resource_request_too_large": lambda: ResourceRequestTooLarge(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ),
            "resource_request_too_small": lambda: ResourceRequestTooSmall(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ),
            "wrong_service_selector_astronomy_shop": lambda: WrongServiceSelector(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "wrong_service_selector_hotel_reservation": lambda: WrongServiceSelector(
                app_name="hotel_reservation", faulty_service="frontend"
            ),
            "wrong_service_selector_social_network": lambda: WrongServiceSelector(
                app_name="social_network", faulty_service="user-service"
            ),
            "service_dns_resolution_failure_astronomy_shop": lambda: ServiceDNSResolutionFailure(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "service_dns_resolution_failure_social_network": lambda: ServiceDNSResolutionFailure(
                app_name="social_network", faulty_service="user-service"
            ),
            "wrong_dns_policy_astronomy_shop": lambda: WrongDNSPolicy(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "wrong_dns_policy_social_network": lambda: WrongDNSPolicy(
                app_name="social_network", faulty_service="user-service"
            ),
            "wrong_dns_policy_hotel_reservation": lambda: WrongDNSPolicy(
                app_name="hotel_reservation", faulty_service="profile"
            ),
            "stale_coredns_config_astronomy_shop": lambda: StaleCoreDNSConfig(app_name="astronomy_shop"),
            "stale_coredns_config_social_network": lambda: StaleCoreDNSConfig(app_name="social_network"),
            "sidecar_port_conflict_astronomy_shop": lambda: SidecarPortConflict(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "sidecar_port_conflict_social_network": lambda: SidecarPortConflict(
                app_name="social_network", faulty_service="user-service"
            ),
            "sidecar_port_conflict_hotel_reservation": lambda: SidecarPortConflict(
                app_name="hotel_reservation", faulty_service="frontend"
            ),
            "env_variable_leak_social_network": lambda: EnvVariableLeak(
                app_name="social_network", faulty_service="media-mongodb"
            ),
            "env_variable_leak_hotel_reservation": lambda: EnvVariableLeak(
                app_name="hotel_reservation", faulty_service="mongodb-geo"
            ),
            "configmap_drift_hotel_reservation": lambda: ConfigMapDrift(faulty_service="geo"),
            "readiness_probe_misconfiguration_astronomy_shop": lambda: ReadinessProbeMisconfiguration(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "readiness_probe_misconfiguration_social_network": lambda: ReadinessProbeMisconfiguration(
                app_name="social_network", faulty_service="user-service"
            ),
            "readiness_probe_misconfiguration_hotel_reservation": lambda: ReadinessProbeMisconfiguration(
                app_name="hotel_reservation", faulty_service="frontend"
            ),
            "liveness_probe_misconfiguration_astronomy_shop": lambda: LivenessProbeMisconfiguration(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "liveness_probe_misconfiguration_social_network": lambda: LivenessProbeMisconfiguration(
                app_name="social_network", faulty_service="user-service"
            ),
            "liveness_probe_misconfiguration_hotel_reservation": lambda: LivenessProbeMisconfiguration(
                app_name="hotel_reservation", faulty_service="recommendation"
            ),
            "network_policy_block": lambda: NetworkPolicyBlock(
                faulty_service="payment-service"
            ),
            "liveness_probe_too_aggressive_astronomy_shop": lambda: LivenessProbeTooAggressive(
                app_name="astronomy_shop"
            ),
            "liveness_probe_too_aggressive_social_network": lambda: LivenessProbeTooAggressive(
                app_name="social_network"
            ),
            "liveness_probe_too_aggressive_hotel_reservation": lambda: LivenessProbeTooAggressive(
                app_name="hotel_reservation"
            ),
            "duplicate_pvc_mounts_astronomy_shop": lambda: DuplicatePVCMounts(
                app_name="astronomy_shop", faulty_service="frontend"
            ),
            "duplicate_pvc_mounts_social_network": lambda: DuplicatePVCMounts(
                app_name="social_network", faulty_service="jaeger"
            ),
            "duplicate_pvc_mounts_hotel_reservation": lambda: DuplicatePVCMounts(
                app_name="hotel_reservation", faulty_service="frontend"
            ),
            "env_variable_shadowing_astronomy_shop": lambda: EnvVariableShadowing(),
            "rolling_update_misconfigured_social_network": lambda: RollingUpdateMisconfigured(
                app_name="social_network"),
            "rolling_update_misconfigured_hotel_reservation": lambda: RollingUpdateMisconfigured(
                app_name="hotel_reservation"),
            "ingress_misroute": lambda: IngressMisroute(
                path="/api",
                correct_service="frontend-service",
                wrong_service="recommendation-service"),

            # "missing_service_astronomy_shop": lambda: MissingService(app_name="astronomy_shop", faulty_service="ad"),
            # K8S operator misoperation -> Refactor later, not sure if they're working
            # They will also need to be updated to the new problem format.
            # "operator_overload_replicas-detection-1": K8SOperatorOverloadReplicasDetection,
            # "operator_overload_replicas-localization-1": K8SOperatorOverloadReplicasLocalization,
            # "operator_non_existent_storage-detection-1": K8SOperatorNonExistentStorageDetection,
            # "operator_non_existent_storage-localization-1": K8SOperatorNonExistentStorageLocalization,
            # "operator_invalid_affinity_toleration-detection-1": K8SOperatorInvalidAffinityTolerationDetection,
            # "operator_invalid_affinity_toleration-localization-1": K8SOperatorInvalidAffinityTolerationLocalization,
            # "operator_security_context_fault-detection-1": K8SOperatorSecurityContextFaultDetection,
            # "operator_security_context_fault-localization-1": K8SOperatorSecurityContextFaultLocalization,
            # "operator_wrong_update_strategy-detection-1": K8SOperatorWrongUpdateStrategyDetection,
            # "operator_wrong_update_strategy-localization-1": K8SOperatorWrongUpdateStrategyLocalization,
        }

    def get_problem_instance(self, problem_id: str):
        if problem_id not in self.PROBLEM_REGISTRY:
            raise ValueError(f"Problem ID {problem_id} not found in registry.")

        return self.PROBLEM_REGISTRY.get(problem_id)()

    def get_problem(self, problem_id: str):
        return self.PROBLEM_REGISTRY.get(problem_id)

    def get_problem_ids(self, task_type: str = None):
        if task_type:
            return [k for k in self.PROBLEM_REGISTRY.keys() if task_type in k]
        return list(self.PROBLEM_REGISTRY.keys())

    def get_problem_count(self, task_type: str = None):
        if task_type:
            return len([k for k in self.PROBLEM_REGISTRY.keys() if task_type in k])
        return len(self.PROBLEM_REGISTRY)
