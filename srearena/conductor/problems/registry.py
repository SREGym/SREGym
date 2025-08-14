from srearena.conductor.problems.ad_service_failure import AdServiceFailure
from srearena.conductor.problems.ad_service_high_cpu import AdServiceHighCpu
from srearena.conductor.problems.ad_service_manual_gc import AdServiceManualGc
from srearena.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from srearena.conductor.problems.auth_miss_mongodb import MongoDBAuthMissing
from srearena.conductor.problems.cart_service_failure import CartServiceFailure
from srearena.conductor.problems.configmap_drift import ConfigMapDrift
from srearena.conductor.problems.container_kill import ChaosMeshContainerKill
from srearena.conductor.problems.cpu_stress import ChaosMeshCPUStress
from srearena.conductor.problems.duplicate_pvc_mounts import DuplicatePVCMounts
from srearena.conductor.problems.env_variable_leak import EnvVariableLeak
from srearena.conductor.problems.env_variable_shadowing import EnvVariableShadowing
from srearena.conductor.problems.http_abort import ChaosMeshHttpAbort
from srearena.conductor.problems.http_post_tamper import ChaosMeshHttpPostTamper
from srearena.conductor.problems.image_slow_load import ImageSlowLoad
from srearena.conductor.problems.incorrect_image import IncorrectImage
from srearena.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from srearena.conductor.problems.ingress_misroute import IngressMisroute
from srearena.conductor.problems.jvm_heap_stress import ChaosMeshJVMHeapStress
from srearena.conductor.problems.jvm_return import ChaosMeshJVMReturnFault
from srearena.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from srearena.conductor.problems.liveness_probe_misconfiguration import LivenessProbeMisconfiguration
from srearena.conductor.problems.liveness_probe_too_aggressive import LivenessProbeTooAggressive
from srearena.conductor.problems.loadgenerator_flood_homepage import LoadGeneratorFloodHomepage
from srearena.conductor.problems.memory_stress import ChaosMeshMemoryStress
from srearena.conductor.problems.misconfig_app import MisconfigAppHotelRes
from srearena.conductor.problems.missing_service import MissingService
from srearena.conductor.problems.namespace_memory_limit import NamespaceMemoryLimit
from srearena.conductor.problems.network_delay import ChaosMeshNetworkDelay
from srearena.conductor.problems.network_loss import ChaosMeshNetworkLoss
from srearena.conductor.problems.network_partition import ChaosMeshNetworkPartition
from srearena.conductor.problems.network_policy_block import NetworkPolicyBlock
from srearena.conductor.problems.payment_service_failure import PaymentServiceFailure
from srearena.conductor.problems.payment_service_unreachable import PaymentServiceUnreachable
from srearena.conductor.problems.persistent_volume_affinity_violation import PersistentVolumeAffinityViolation
from srearena.conductor.problems.pod_anti_affinity_deadlock import PodAntiAffinityDeadlock
from srearena.conductor.problems.pod_failure import ChaosMeshPodFailure
from srearena.conductor.problems.pod_kill import ChaosMeshPodKill
from srearena.conductor.problems.product_catalog_failure import ProductCatalogServiceFailure
from srearena.conductor.problems.readiness_probe_misconfiguration import ReadinessProbeMisconfiguration
from srearena.conductor.problems.recommendation_service_cache_failure import RecommendationServiceCacheFailure
from srearena.conductor.problems.redeploy_without_pv import RedeployWithoutPV
from srearena.conductor.problems.resource_request import ResourceRequestTooLarge, ResourceRequestTooSmall
from srearena.conductor.problems.revoke_auth import MongoDBRevokeAuth
from srearena.conductor.problems.rolling_update_misconfigured import RollingUpdateMisconfigured
from srearena.conductor.problems.scale_pod import ScalePodSocialNet
from srearena.conductor.problems.service_dns_resolution_failure import ServiceDNSResolutionFailure
from srearena.conductor.problems.sidecar_port_conflict import SidecarPortConflict
from srearena.conductor.problems.stale_coredns_config import StaleCoreDNSConfig
from srearena.conductor.problems.storage_user_unregistered import MongoDBUserUnregistered
from srearena.conductor.problems.taint_no_toleration import TaintNoToleration
from srearena.conductor.problems.target_port import K8STargetPortMisconfig
from srearena.conductor.problems.valkey_auth_disruption import ValkeyAuthDisruption
from srearena.conductor.problems.valkey_memory_disruption import ValkeyMemoryDisruption
from srearena.conductor.problems.wrong_bin_usage import WrongBinUsage
from srearena.conductor.problems.wrong_dns_policy import WrongDNSPolicy
from srearena.conductor.problems.wrong_service_selector import WrongServiceSelector


class ProblemRegistry:
    def __init__(self):
        self.PROBLEM_REGISTRY = {
            "k8s_target_port-misconfig": {"creator": lambda: K8STargetPortMisconfig(faulty_service="user-service"), "agnostic": False},
            "auth_miss_mongodb": {"creator": MongoDBAuthMissing, "agnostic": False},
            "revoke_auth_mongodb-1": {"creator": lambda: MongoDBRevokeAuth(faulty_service="mongodb-geo"), "agnostic": False},
            "revoke_auth_mongodb-2": {"creator": lambda: MongoDBRevokeAuth(faulty_service="mongodb-rate"), "agnostic": False},
            "storage_user_unregistered-1": {"creator": lambda: MongoDBUserUnregistered(faulty_service="mongodb-geo"), "agnostic": False},
            "storage_user_unregistered-2": {"creator": lambda: MongoDBUserUnregistered(faulty_service="mongodb-rate"), "agnostic": False},
            "misconfig_app_hotel_res": {"creator": MisconfigAppHotelRes, "agnostic": False},
            "scale_pod_zero_social_net": {"creator": ScalePodSocialNet, "agnostic": False},
            "assign_to_non_existent_node": {"creator": AssignNonExistentNode, "agnostic": False},
	        "pod_anti_affinity_deadlock": {"creator": PodAntiAffinityDeadlock, "agnostic": False},
            "chaos_mesh_container_kill": {"creator": ChaosMeshContainerKill, "agnostic": False},
            "chaos_mesh_pod_failure": {"creator": ChaosMeshPodFailure, "agnostic": False},
            "chaos_mesh_pod_kill": {"creator": ChaosMeshPodKill, "agnostic": False},
            "chaos_mesh_network_loss": {"creator": ChaosMeshNetworkLoss, "agnostic": False},
            "chaos_mesh_network_delay": {"creator": ChaosMeshNetworkDelay, "agnostic": False},
            "chaos_mesh_network_partition": {"creator": ChaosMeshNetworkPartition, "agnostic": False},
            "chaos_mesh_http_abort": {"creator": ChaosMeshHttpAbort, "agnostic": False},
            "chaos_mesh_cpu_stress": {"creator": ChaosMeshCPUStress, "agnostic": False},
            "chaos_mesh_jvm_stress": {"creator": ChaosMeshJVMHeapStress, "agnostic": False},
            "chaos_mesh_jvm_return": {"creator": ChaosMeshJVMReturnFault, "agnostic": False},
            "chaos_mesh_memory_stress": {"creator": ChaosMeshMemoryStress, "agnostic": False},
            "chaos_mesh_http_post_tamper": {"creator": ChaosMeshHttpPostTamper, "agnostic": False},
            "astronomy_shop_ad_service_failure": {"creator": AdServiceFailure, "agnostic": False},
            "astronomy_shop_ad_service_high_cpu": {"creator": AdServiceHighCpu, "agnostic": False},
            "astronomy_shop_ad_service_manual_gc": {"creator": AdServiceManualGc, "agnostic": False},
            "astronomy_shop_kafka_queue_problems": {"creator": KafkaQueueProblems, "agnostic": False},
            "astronomy_shop_cart_service_failure": {"creator": CartServiceFailure, "agnostic": False},
            "astronomy_shop_image_slow_load": {"creator": ImageSlowLoad, "agnostic": False},
            "astronomy_shop_loadgenerator_flood_homepage": {"creator": LoadGeneratorFloodHomepage, "agnostic": False},
            "astronomy_shop_payment_service_failure": {"creator": PaymentServiceFailure, "agnostic": False},
            "astronomy_shop_payment_service_unreachable": {"creator": PaymentServiceUnreachable, "agnostic": False},
            "astronomy_shop_product_catalog_service_failure": {"creator": ProductCatalogServiceFailure, "agnostic": False},
            "astronomy_shop_recommendation_service_cache_failure": {"creator": RecommendationServiceCacheFailure, "agnostic": False},
            "redeploy_without_PV": {"creator": RedeployWithoutPV, "agnostic": False},
            "wrong_bin_usage": {"creator": WrongBinUsage, "agnostic": False},
            "taint_no_toleration_social_network": {"creator": lambda: TaintNoToleration(), "agnostic": False},
            "missing_service_hotel_reservation": {"creator": lambda: MissingService(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ), "agnostic": False},
            "missing_service_social_network": {"creator": lambda: MissingService(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "resource_request_too_large": {"creator": lambda: ResourceRequestTooLarge(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ), "agnostic": False},
            "resource_request_too_small": {"creator": lambda: ResourceRequestTooSmall(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ), "agnostic": False},
            "wrong_service_selector_astronomy_shop": {"creator": lambda: WrongServiceSelector(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "wrong_service_selector_hotel_reservation": {"creator": lambda: WrongServiceSelector(
                app_name="hotel_reservation", faulty_service="frontend"
            ), "agnostic": False},
            "wrong_service_selector_social_network": {"creator": lambda: WrongServiceSelector(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "service_dns_resolution_failure_astronomy_shop": {"creator": lambda: ServiceDNSResolutionFailure(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "service_dns_resolution_failure_social_network": {"creator": lambda: ServiceDNSResolutionFailure(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "wrong_dns_policy_astronomy_shop": {"creator": lambda: WrongDNSPolicy(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "wrong_dns_policy_social_network": {"creator": lambda: WrongDNSPolicy(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "wrong_dns_policy_hotel_reservation": {"creator": lambda: WrongDNSPolicy(
                app_name="hotel_reservation", faulty_service="profile"
            ), "agnostic": False},
            "stale_coredns_config_astronomy_shop": {"creator": lambda: StaleCoreDNSConfig(app_name="astronomy_shop"), "agnostic": False},
            "stale_coredns_config_social_network": {"creator": lambda: StaleCoreDNSConfig(app_name="social_network"), "agnostic": False},
            "sidecar_port_conflict_astronomy_shop": {"creator": lambda: SidecarPortConflict(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "sidecar_port_conflict_social_network": {"creator": lambda: SidecarPortConflict(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "sidecar_port_conflict_hotel_reservation": {"creator": lambda: SidecarPortConflict(
                app_name="hotel_reservation", faulty_service="frontend"
            ), "agnostic": False},
            "env_variable_leak_social_network": {"creator": lambda: EnvVariableLeak(
                app_name="social_network", faulty_service="media-mongodb"
            ), "agnostic": False},
            "env_variable_leak_hotel_reservation": {"creator": lambda: EnvVariableLeak(
                app_name="hotel_reservation", faulty_service="mongodb-geo"
            ), "agnostic": False},
            "configmap_drift_hotel_reservation": {"creator": lambda: ConfigMapDrift(faulty_service="geo"), "agnostic": False},
            "readiness_probe_misconfiguration_astronomy_shop": {"creator": lambda: ReadinessProbeMisconfiguration(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "readiness_probe_misconfiguration_social_network": {"creator": lambda: ReadinessProbeMisconfiguration(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "readiness_probe_misconfiguration_hotel_reservation": {"creator": lambda: ReadinessProbeMisconfiguration(
                app_name="hotel_reservation", faulty_service="frontend"
            ), "agnostic": False},
            "liveness_probe_misconfiguration_astronomy_shop": {"creator": lambda: LivenessProbeMisconfiguration(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "liveness_probe_misconfiguration_social_network": {"creator": lambda: LivenessProbeMisconfiguration(
                app_name="social_network", faulty_service="user-service"
            ), "agnostic": False},
            "liveness_probe_misconfiguration_hotel_reservation": {"creator": lambda: LivenessProbeMisconfiguration(
                app_name="hotel_reservation", faulty_service="recommendation"
            ), "agnostic": False},
            "network_policy_block": {"creator": lambda: NetworkPolicyBlock(faulty_service="payment-service"), "agnostic": False},
            "liveness_probe_too_aggressive_astronomy_shop": {"creator": lambda: LivenessProbeTooAggressive(
                app_name="astronomy_shop"
            ), "agnostic": False},
            "liveness_probe_too_aggressive_social_network": {"creator": lambda: LivenessProbeTooAggressive(
                app_name="social_network"
            ), "agnostic": False},
            "liveness_probe_too_aggressive_hotel_reservation": {"creator": lambda: LivenessProbeTooAggressive(
                app_name="hotel_reservation"
            ), "agnostic": False},
            "duplicate_pvc_mounts_astronomy_shop": {"creator": lambda: DuplicatePVCMounts(
                app_name="astronomy_shop", faulty_service="frontend"
            ), "agnostic": False},
            "duplicate_pvc_mounts_social_network": {"creator": lambda: DuplicatePVCMounts(
                app_name="social_network", faulty_service="jaeger"
            ), "agnostic": False},
            "duplicate_pvc_mounts_hotel_reservation": {"creator": lambda: DuplicatePVCMounts(
                app_name="hotel_reservation", faulty_service="frontend"
            ), "agnostic": False},
            "env_variable_shadowing_astronomy_shop": {"creator": lambda: EnvVariableShadowing(), "agnostic": False},
            "rolling_update_misconfigured_social_network": {"creator": lambda: RollingUpdateMisconfigured(
                app_name="social_network"
            ), "agnostic": False},
            "rolling_update_misconfigured_hotel_reservation": {"creator": lambda: RollingUpdateMisconfigured(
                app_name="hotel_reservation"
            ), "agnostic": False},
            "ingress_misroute": {"creator": lambda: IngressMisroute(
                path="/api", correct_service="frontend-service", wrong_service="recommendation-service"
            ), "agnostic": False},
            "persistent_volume_affinity_violation": {"creator": PersistentVolumeAffinityViolation, "agnostic": False},
            "valkey_auth_disruption": {"creator": ValkeyAuthDisruption, "agnostic": False},
            "valkey_memory_disruption": {"creator": ValkeyMemoryDisruption, "agnostic": False},
            "incorrect_port_assignment": {"creator": IncorrectPortAssignment, "agnostic": False},
            "incorrect_image": {"creator": IncorrectImage, "agnostic": False},
            "namespace_memory_limit": {"creator": NamespaceMemoryLimit, "agnostic": False},
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

        problem_config = self.PROBLEM_REGISTRY.get(problem_id)
        return problem_config["creator"]()

    def get_problem(self, problem_id: str):
        problem_config = self.PROBLEM_REGISTRY.get(problem_id)
        if problem_config:
            return problem_config["creator"]
        return None

    def get_problem_ids(self, task_type: str = None):
        if task_type:
            return [k for k in self.PROBLEM_REGISTRY.keys() if task_type in k]
        return list(self.PROBLEM_REGISTRY.keys())

    def get_problem_count(self, task_type: str = None):
        if task_type:
            return len([k for k in self.PROBLEM_REGISTRY.keys() if task_type in k])
        return len(self.PROBLEM_REGISTRY)
