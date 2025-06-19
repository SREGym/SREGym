from typing import List

from srearena.conductor.problems.ad_service_failure import AdServiceFailure
from srearena.conductor.problems.ad_service_high_cpu import AdServiceHighCpu
from srearena.conductor.problems.ad_service_manual_gc import AdServiceManualGc
from srearena.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from srearena.conductor.problems.auth_miss_mongodb import MongoDBAuthMissing
from srearena.conductor.problems.base import Problem
from srearena.conductor.problems.cart_service_failure import CartServiceFailure
from srearena.conductor.problems.container_kill import ChaosMeshContainerKill
from srearena.conductor.problems.image_slow_load import ImageSlowLoad
from srearena.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from srearena.conductor.problems.loadgenerator_flood_homepage import LoadGeneratorFloodHomepage
from srearena.conductor.problems.misconfig_app import MisconfigAppHotelRes
from srearena.conductor.problems.missing_service import MissingService
from srearena.conductor.problems.network_delay import ChaosMeshNetworkDelay
from srearena.conductor.problems.network_loss import ChaosMeshNetworkLoss
from srearena.conductor.problems.payment_service_failure import PaymentServiceFailure
from srearena.conductor.problems.payment_service_unreachable import PaymentServiceUnreachable
from srearena.conductor.problems.pod_failure import ChaosMeshPodFailure
from srearena.conductor.problems.pod_kill import ChaosMeshPodKill
from srearena.conductor.problems.port_misconfiguration import PortMisconfiguration
from srearena.conductor.problems.product_catalog_failure import ProductCatalogServiceFailure
from srearena.conductor.problems.recommendation_service_cache_failure import RecommendationServiceCacheFailure
from srearena.conductor.problems.redeploy_without_pv import RedeployWithoutPV
from srearena.conductor.problems.resource_request import ResourceRequestTooLarge, ResourceRequestTooSmall
from srearena.conductor.problems.revoke_auth import MongoDBRevokeAuth
from srearena.conductor.problems.scale_pod import ScalePodSocialNet
from srearena.conductor.problems.service_dns_resolution_failure import ServiceDNSResolutionFailure
from srearena.conductor.problems.sidecar_port_conflict import SidecarPortConflict
from srearena.conductor.problems.stale_coredns_config import StaleCoreDNSConfig
from srearena.conductor.problems.storage_user_unregistered import MongoDBUserUnregistered
from srearena.conductor.problems.target_port import K8STargetPortMisconfig
from srearena.conductor.problems.trainticket_f1_async_message_order import TrainTicketF1AsyncMessageOrderProblem
from srearena.conductor.problems.wrong_bin_usage import WrongBinUsage
from srearena.conductor.problems.wrong_dns_policy import WrongDNSPolicy
from srearena.conductor.problems.wrong_service_selector import WrongServiceSelector


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
            "astronomy_shop_cart_service_failure": CartServiceFailure,
            "astronomy_shop_ad_service_image_slow_load": ImageSlowLoad,
            "astronomy_shop_port_misconfiguration": PortMisconfiguration,
            "astronomy_shop_payment_service_failure": PaymentServiceFailure,
            "astronomy_shop_payment_service_unreachable": PaymentServiceUnreachable,
            "astronomy_shop_product_catalog_service_failure": ProductCatalogServiceFailure,
            "astronomy_shop_recommendation_service_cache_failure": RecommendationServiceCacheFailure,
            "redeploy_without_PV": RedeployWithoutPV,
            "wrong_bin_usage": WrongBinUsage,
            "trainticket_f1_async_message_order": TrainTicketF1AsyncMessageOrderProblem,
            "missing_service_hotel_reservation": lambda: MissingService(
                app_name="hotel_reservation", faulty_service="mongodb-rate"
            ),
            "kafka_queue_problems_hotel_reservation": lambda: KafkaQueueProblems(
                app_name="hotel_reservation", faulty_service="memcached-rate"
            ),
            "loadgenerator_flood_homepage": LoadGeneratorFloodHomepage,
            "k8s_dns_resolution_failure": ServiceDNSResolutionFailure,
            "k8s_sidecar_port_conflict": SidecarPortConflict,
            "k8s_stale_coredns_config": StaleCoreDNSConfig,
            "k8s_resource_request_too_large": ResourceRequestTooLarge,
            "k8s_resource_request_too_small": ResourceRequestTooSmall,
            "k8s_wrong_dns_policy": WrongDNSPolicy,
            "k8s_wrong_service_selector": WrongServiceSelector,
        }

    def list_problems(self) -> List[str]:
        """List all available problem IDs.
        
        Returns:
            List[str]: List of problem IDs
        """
        return list(self.PROBLEM_REGISTRY.keys())

    def get_problem_instance(self, problem_id: str) -> Problem:
        """Get an instance of a problem by ID.
        
        Args:
            problem_id: The ID of the problem to instantiate
            
        Returns:
            Problem: An instance of the requested problem
            
        Raises:
            ValueError: If the problem ID is not found in the registry
        """
        if problem_id not in self.PROBLEM_REGISTRY:
            raise ValueError(f"Problem ID {problem_id} not found in registry.")
        
        problem_class_or_factory = self.PROBLEM_REGISTRY.get(problem_id)
        
        # Check if it's a lambda (factory function) or a class
        if callable(problem_class_or_factory):
            return problem_class_or_factory()
        else:
            return problem_class_or_factory()
