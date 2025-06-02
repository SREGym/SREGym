from srearena.conductor.problems.ad_service_failure import AdServiceFailure
from srearena.conductor.problems.ad_service_high_cpu import AdServiceHighCpu
from srearena.conductor.problems.ad_service_manual_gc import AdServiceManualGc
from srearena.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from srearena.conductor.problems.auth_miss_mongodb import MongoDBAuthMissing
from srearena.conductor.problems.cart_service_failure import CartServiceFailure
from srearena.conductor.problems.container_kill import ChaosMeshContainerKill
from srearena.conductor.problems.image_slow_load import ImageSlowLoad
from srearena.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from srearena.conductor.problems.loadgenerator_flood_homepage import LoadGeneratorFloodHomepage
from srearena.conductor.problems.misconfig_app import MisconfigAppHotelRes
from srearena.conductor.problems.network_delay import ChaosMeshNetworkDelay
from srearena.conductor.problems.network_loss import ChaosMeshNetworkLoss
from srearena.conductor.problems.no_op import NoOp
from srearena.conductor.problems.payment_service_failure import PaymentServiceFailure
from srearena.conductor.problems.payment_service_unreachable import PaymentServiceUnreachable
from srearena.conductor.problems.pod_failure import ChaosMeshPodFailure
from srearena.conductor.problems.pod_kill import ChaosMeshPodKill
from srearena.conductor.problems.product_catalog_failure import ProductCatalogServiceFailure
from srearena.conductor.problems.recommendation_service_cache_failure import RecommendationServiceCacheFailure
from srearena.conductor.problems.redeploy_without_pv import RedeployWithoutPV
from srearena.conductor.problems.revoke_auth import MongoDBRevokeAuth
from srearena.conductor.problems.scale_pod import ScalePodSocialNet
from srearena.conductor.problems.storage_user_unregistered import MongoDBUserUnregistered
from srearena.conductor.problems.target_port import K8STargetPortMisconfig
from srearena.conductor.problems.wrong_bin_usage import WrongBinUsage


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
            "noop_hotel_reservation": lambda: NoOp(app_name="hotel_reservation"),
            "noop_social_network": lambda: NoOp(app_name="social_network"),
            "noop_astronomy_shop": lambda: NoOp(app_name="astronomy_shop"),
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
