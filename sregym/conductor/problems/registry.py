from pathlib import Path

import yaml

from sregym.conductor.problems.ad_service_failure import AdServiceFailure
from sregym.conductor.problems.ad_service_high_cpu import AdServiceHighCpu
from sregym.conductor.problems.ad_service_manual_gc import AdServiceManualGc
from sregym.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from sregym.conductor.problems.auth_miss_mongodb import MongoDBAuthMissing
from sregym.conductor.problems.capacity_decrease_rpc_retry_storm import CapacityDecreaseRPCRetryStorm
from sregym.conductor.problems.cart_service_failure import CartServiceFailure
from sregym.conductor.problems.configmap_drift import ConfigMapDrift
from sregym.conductor.problems.duplicate_pvc_mounts import DuplicatePVCMounts
from sregym.conductor.problems.env_variable_shadowing import EnvVariableShadowing
from sregym.conductor.problems.faulty_image_correlated import FaultyImageCorrelated
from sregym.conductor.problems.gc_capacity_degradation import GCCapacityDegradation
from sregym.conductor.problems.image_slow_load import ImageSlowLoad
from sregym.conductor.problems.incorrect_image import IncorrectImage
from sregym.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from sregym.conductor.problems.ingress_misroute import IngressMisroute
from sregym.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from sregym.conductor.problems.khaos_faults import KhaosFaultName, KhaosFaultProblem
from sregym.conductor.problems.kubelet_crash import KubeletCrash
from sregym.conductor.problems.latent_sector_error import LatentSectorError
from sregym.conductor.problems.liveness_probe_misconfiguration import LivenessProbeMisconfiguration
from sregym.conductor.problems.liveness_probe_too_aggressive import LivenessProbeTooAggressive
from sregym.conductor.problems.load_spike_rpc_retry_storm import LoadSpikeRPCRetryStorm
from sregym.conductor.problems.loadgenerator_flood_homepage import LoadGeneratorFloodHomepage
from sregym.conductor.problems.misconfig_app import MisconfigAppHotelRes
from sregym.conductor.problems.missing_configmap import MissingConfigMap
from sregym.conductor.problems.missing_env_variable import MissingEnvVariable
from sregym.conductor.problems.missing_service import MissingService
from sregym.conductor.problems.multiple_failures import MultipleIndependentFailures
from sregym.conductor.problems.namespace_memory_limit import NamespaceMemoryLimit
from sregym.conductor.problems.network_policy_block import NetworkPolicyBlock
from sregym.conductor.problems.operator_misoperation.invalid_affinity_toleration import (
    K8SOperatorInvalidAffinityTolerationFault,
)
from sregym.conductor.problems.operator_misoperation.non_existent_storage import K8SOperatorNonExistentStorageFault
from sregym.conductor.problems.operator_misoperation.overload_replicas import K8SOperatorOverloadReplicasFault
from sregym.conductor.problems.operator_misoperation.security_context_fault import K8SOperatorSecurityContextFault
from sregym.conductor.problems.operator_misoperation.wrong_update_strategy import K8SOperatorWrongUpdateStrategyFault
from sregym.conductor.problems.payment_service_failure import PaymentServiceFailure
from sregym.conductor.problems.payment_service_unreachable import PaymentServiceUnreachable
from sregym.conductor.problems.persistent_volume_affinity_violation import PersistentVolumeAffinityViolation
from sregym.conductor.problems.pod_anti_affinity_deadlock import PodAntiAffinityDeadlock
from sregym.conductor.problems.product_catalog_failure import ProductCatalogServiceFailure
from sregym.conductor.problems.pvc_claim_mismatch import PVCClaimMismatch
from sregym.conductor.problems.rbac_misconfiguration import RBACMisconfiguration
from sregym.conductor.problems.readiness_probe_misconfiguration import ReadinessProbeMisconfiguration
from sregym.conductor.problems.recommendation_service_cache_failure import RecommendationServiceCacheFailure
from sregym.conductor.problems.resource_request import ResourceRequestTooLarge, ResourceRequestTooSmall
from sregym.conductor.problems.revoke_auth import MongoDBRevokeAuth
from sregym.conductor.problems.rolling_update_misconfigured import RollingUpdateMisconfigured
from sregym.conductor.problems.scale_pod import ScalePodSocialNet
from sregym.conductor.problems.service_dns_resolution_failure import ServiceDNSResolutionFailure
from sregym.conductor.problems.sidecar_port_conflict import SidecarPortConflict
from sregym.conductor.problems.silent_data_corruption import SilentDataCorruption
from sregym.conductor.problems.stale_coredns_config import StaleCoreDNSConfig
from sregym.conductor.problems.storage_user_unregistered import MongoDBUserUnregistered
from sregym.conductor.problems.taint_no_toleration import TaintNoToleration
from sregym.conductor.problems.target_port import K8STargetPortMisconfig
from sregym.conductor.problems.train_ticket_f22 import TrainTicketF22
from sregym.conductor.problems.trainticket_f17 import TrainTicketF17
from sregym.conductor.problems.update_incompatible_correlated import UpdateIncompatibleCorrelated
from sregym.conductor.problems.valkey_auth_disruption import ValkeyAuthDisruption
from sregym.conductor.problems.valkey_memory_disruption import ValkeyMemoryDisruption
from sregym.conductor.problems.workload_imbalance import WorkloadImbalance
from sregym.conductor.problems.wrong_bin_usage import WrongBinUsage
from sregym.conductor.problems.wrong_dns_policy import WrongDNSPolicy
from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.service.kubectl import KubeCtl


# fmt: off
class ProblemRegistry:
    def __init__(self):
        self.PROBLEM_REGISTRY = {
            # ==================== APPLICATION FAULT INJECTOR ====================
            # --- CORRELATED PROBLEMS ---
            "faulty_image_correlated": FaultyImageCorrelated,
            "update_incompatible_correlated": UpdateIncompatibleCorrelated,
            # --- REGULAR APPLICATION PROBLEMS ---
            "incorrect_image": IncorrectImage,
            "incorrect_port_assignment": IncorrectPortAssignment,
            "misconfig_app_hotel_res": MisconfigAppHotelRes,
            "missing_env_variable_astronomy_shop": lambda: MissingEnvVariable(app_name="astronomy_shop", faulty_service="frontend" ),
            "revoke_auth_mongodb-1": lambda: MongoDBRevokeAuth(faulty_service="mongodb-geo"),
            "revoke_auth_mongodb-2": lambda: MongoDBRevokeAuth(faulty_service="mongodb-rate"),
            "storage_user_unregistered-1": lambda: MongoDBUserUnregistered(faulty_service="mongodb-geo"),
            "storage_user_unregistered-2": lambda: MongoDBUserUnregistered(faulty_service="mongodb-rate"),
            "valkey_auth_disruption": ValkeyAuthDisruption,
            "valkey_memory_disruption": ValkeyMemoryDisruption,
            # # ==================== VIRTUALIZATION FAULT INJECTOR ====================
            # --- METASTABLE FAILURES ---
            "capacity_decrease_rpc_retry_storm": CapacityDecreaseRPCRetryStorm,
            "gc_capacity_degradation": GCCapacityDegradation,
            "load_spike_rpc_retry_storm": LoadSpikeRPCRetryStorm,
            # --- REGULAR VIRTUALIZATION PROBLEMS ---
            "assign_to_non_existent_node": AssignNonExistentNode,
            "auth_miss_mongodb": MongoDBAuthMissing,
            "configmap_drift_hotel_reservation": lambda: ConfigMapDrift(faulty_service="geo"),
            "duplicate_pvc_mounts_astronomy_shop": lambda: DuplicatePVCMounts(app_name="astronomy_shop", faulty_service="frontend"),
            "duplicate_pvc_mounts_hotel_reservation": lambda: DuplicatePVCMounts(app_name="hotel_reservation", faulty_service="frontend"),
            "duplicate_pvc_mounts_social_network": lambda: DuplicatePVCMounts(app_name="social_network", faulty_service="jaeger"),
            "env_variable_shadowing_astronomy_shop": lambda: EnvVariableShadowing(),
            "k8s_target_port-misconfig": lambda: K8STargetPortMisconfig(faulty_service="user-service"),
            "liveness_probe_misconfiguration_astronomy_shop": lambda: LivenessProbeMisconfiguration(app_name="astronomy_shop", faulty_service="frontend"),
            "liveness_probe_misconfiguration_hotel_reservation": lambda: LivenessProbeMisconfiguration(app_name="hotel_reservation", faulty_service="recommendation"),
            "liveness_probe_misconfiguration_social_network": lambda: LivenessProbeMisconfiguration(app_name="social_network", faulty_service="user-service"),
            "liveness_probe_too_aggressive_astronomy_shop": lambda: LivenessProbeTooAggressive(app_name="astronomy_shop"),
            "liveness_probe_too_aggressive_hotel_reservation": lambda: LivenessProbeTooAggressive(app_name="hotel_reservation"),
            "liveness_probe_too_aggressive_social_network": lambda: LivenessProbeTooAggressive(app_name="social_network"),
            "missing_configmap_hotel_reservation": lambda: MissingConfigMap(app_name="hotel_reservation", faulty_service="mongodb-geo"),
            "missing_configmap_social_network": lambda: MissingConfigMap(app_name="social_network", faulty_service="media-mongodb"),
            "missing_service_astronomy_shop": lambda: MissingService(app_name="astronomy_shop", faulty_service="ad"),
            "missing_service_hotel_reservation": lambda: MissingService(app_name="hotel_reservation", faulty_service="mongodb-rate"),
            "missing_service_social_network": lambda: MissingService(app_name="social_network", faulty_service="user-service"),
            "namespace_memory_limit": NamespaceMemoryLimit,
            "pod_anti_affinity_deadlock": PodAntiAffinityDeadlock,
            "persistent_volume_affinity_violation": PersistentVolumeAffinityViolation,
            "pvc_claim_mismatch": PVCClaimMismatch,
            "rbac_misconfiguration": RBACMisconfiguration,
            "readiness_probe_misconfiguration_astronomy_shop": lambda: ReadinessProbeMisconfiguration(app_name="astronomy_shop", faulty_service="frontend"),
            "readiness_probe_misconfiguration_hotel_reservation": lambda: ReadinessProbeMisconfiguration(app_name="hotel_reservation", faulty_service="frontend"),
            "readiness_probe_misconfiguration_social_network": lambda: ReadinessProbeMisconfiguration(app_name="social_network", faulty_service="user-service"),
            "resource_request_too_large": lambda: ResourceRequestTooLarge(app_name="hotel_reservation", faulty_service="mongodb-rate"),
            "resource_request_too_small": lambda: ResourceRequestTooSmall(app_name="hotel_reservation", faulty_service="mongodb-rate"),
            "rolling_update_misconfigured_hotel_reservation": lambda: RollingUpdateMisconfigured(app_name="hotel_reservation"),
            "rolling_update_misconfigured_social_network": lambda: RollingUpdateMisconfigured(app_name="social_network"),
            "scale_pod_zero_social_net": ScalePodSocialNet,
            "service_dns_resolution_failure_astronomy_shop": lambda: ServiceDNSResolutionFailure(app_name="astronomy_shop", faulty_service="frontend"),
            "service_dns_resolution_failure_social_network": lambda: ServiceDNSResolutionFailure(app_name="social_network", faulty_service="user-service"),
            "sidecar_port_conflict_astronomy_shop": lambda: SidecarPortConflict(app_name="astronomy_shop", faulty_service="frontend"),
            "sidecar_port_conflict_hotel_reservation": lambda: SidecarPortConflict(app_name="hotel_reservation", faulty_service="frontend"),
            "sidecar_port_conflict_social_network": lambda: SidecarPortConflict(app_name="social_network", faulty_service="user-service"),
            "stale_coredns_config_astronomy_shop": lambda: StaleCoreDNSConfig(app_name="astronomy_shop"),
            "stale_coredns_config_social_network": lambda: StaleCoreDNSConfig(app_name="social_network"),
            "taint_no_toleration_social_network": lambda: TaintNoToleration(),
            "wrong_bin_usage": WrongBinUsage,
            "wrong_dns_policy_astronomy_shop": lambda: WrongDNSPolicy(app_name="astronomy_shop", faulty_service="frontend"),
            "wrong_dns_policy_hotel_reservation": lambda: WrongDNSPolicy(app_name="hotel_reservation", faulty_service="profile"),
            "wrong_dns_policy_social_network": lambda: WrongDNSPolicy(app_name="social_network", faulty_service="user-service"),
            "wrong_service_selector_astronomy_shop": lambda: WrongServiceSelector(app_name="astronomy_shop", faulty_service="frontend"),
            "wrong_service_selector_hotel_reservation": lambda: WrongServiceSelector(app_name="hotel_reservation", faulty_service="frontend"),
            "wrong_service_selector_social_network": lambda: WrongServiceSelector(app_name="social_network", faulty_service="user-service"),
            # ==================== OPENTELEMETRY FAULT INJECTOR ====================
            "astronomy_shop_ad_service_failure": AdServiceFailure,
            "astronomy_shop_ad_service_high_cpu": AdServiceHighCpu,
            "astronomy_shop_ad_service_manual_gc": AdServiceManualGc,
            "astronomy_shop_cart_service_failure": CartServiceFailure,
            "astronomy_shop_ad_service_image_slow_load": ImageSlowLoad,
            "astronomy_shop_payment_service_failure": PaymentServiceFailure,
            "astronomy_shop_payment_service_unreachable": PaymentServiceUnreachable,
            "astronomy_shop_product_catalog_service_failure": ProductCatalogServiceFailure,
            "astronomy_shop_recommendation_service_cache_failure": RecommendationServiceCacheFailure,
            "kafka_queue_problems": KafkaQueueProblems,
            "loadgenerator_flood_homepage": LoadGeneratorFloodHomepage,
            # ==================== TRAIN TICKET FAULT INJECTOR ====================
            "trainticket_f17_nested_sql_select_clause_error": TrainTicketF17,
            "trainticket_f22_sql_column_name_mismatch_error": TrainTicketF22,
            # ==================== HARDWARE FAULT INJECTOR ====================
            "latent_sector_error": LatentSectorError,
            "silent_data_corruption": SilentDataCorruption,

            "read_error": lambda: KhaosFaultProblem(KhaosFaultName.read_error),
            # "pread_error": lambda: KhaosFaultProblem(KhaosFaultName.pread_error),
            # "write_error": lambda: KhaosFaultProblem(KhaosFaultName.write_error),
            # "pwrite_error": lambda: KhaosFaultProblem(KhaosFaultName.pwrite_error),
            # "fsync_error": lambda: KhaosFaultProblem(KhaosFaultName.fsync_error),
            # "open_error": lambda: KhaosFaultProblem(KhaosFaultName.open_error),
            # "close_fail": lambda: KhaosFaultProblem(KhaosFaultName.close_fail),
            # "dup_fail": lambda: KhaosFaultProblem(KhaosFaultName.dup_fail),
            # "getrandom_fail": lambda: KhaosFaultProblem(KhaosFaultName.getrandom_fail),
            # "gettimeofday_fail": lambda: KhaosFaultProblem(KhaosFaultName.gettimeofday_fail),
            # "ioctl_fail": lambda: KhaosFaultProblem(KhaosFaultName.ioctl_fail),
            # "cuda_malloc_fail": lambda: KhaosFaultProblem(KhaosFaultName.cuda_malloc_fail),
            # "getaddrinfo_fail": lambda: KhaosFaultProblem(KhaosFaultName.getaddrinfo_fail),
            # "nanosleep_throttle": lambda: KhaosFaultProblem(KhaosFaultName.nanosleep_throttle),
            # "nanosleep_interrupt": lambda: KhaosFaultProblem(KhaosFaultName.nanosleep_interrupt),
            # "fork_fail": lambda: KhaosFaultProblem(KhaosFaultName.fork_fail),
            # "clock_drift": lambda: KhaosFaultProblem(KhaosFaultName.clock_drift),
            # "setns_fail": lambda: KhaosFaultProblem(KhaosFaultName.setns_fail),
            # "prlimit_fail": lambda: KhaosFaultProblem(KhaosFaultName.prlimit_fail),
            # "socket_block": lambda: KhaosFaultProblem(KhaosFaultName.socket_block),
            # "mmap_fail": lambda: KhaosFaultProblem(KhaosFaultName.mmap_fail),
            # "mmap_oom": lambda: KhaosFaultProblem(KhaosFaultName.mmap_oom),
            # "brk_fail": lambda: KhaosFaultProblem(KhaosFaultName.brk_fail),
            # "mlock_fail": lambda: KhaosFaultProblem(KhaosFaultName.mlock_fail),
            # "bind_enetdown": lambda: KhaosFaultProblem(KhaosFaultName.bind_enetdown),
            # "mount_io_error": lambda: KhaosFaultProblem(KhaosFaultName.mount_io_error),
            # "force_close_ret_err": lambda: KhaosFaultProblem(KhaosFaultName.force_close_ret_err),
            # "force_read_ret_ok": lambda: KhaosFaultProblem(KhaosFaultName.force_read_ret_ok),
            # "force_open_ret_eperm": lambda: KhaosFaultProblem(KhaosFaultName.force_open_ret_eperm),
            # "force_mmap_eagain": lambda: KhaosFaultProblem(KhaosFaultName.force_mmap_eagain),
            # "force_brk_eagain": lambda: KhaosFaultProblem(KhaosFaultName.force_brk_eagain),
            # "force_mlock_eperm": lambda: KhaosFaultProblem(KhaosFaultName.force_mlock_eperm),
            # "force_mprotect_eacces": lambda: KhaosFaultProblem(KhaosFaultName.force_mprotect_eacces),
            # "force_swapon_einval": lambda: KhaosFaultProblem(KhaosFaultName.force_swapon_einval),
            # "oom_memchunk": lambda: KhaosFaultProblem(KhaosFaultName.oom_memchunk),
            # "oom_heapspace": lambda: KhaosFaultProblem(KhaosFaultName.oom_heapspace),
            # "oom_nonswap": lambda: KhaosFaultProblem(KhaosFaultName.oom_nonswap),
            # "hfrag_memchunk": lambda: KhaosFaultProblem(KhaosFaultName.hfrag_memchunk),
            # "hfrag_heapspace": lambda: KhaosFaultProblem(KhaosFaultName.hfrag_heapspace),
            # "ptable_permit": lambda: KhaosFaultProblem(KhaosFaultName.ptable_permit),
            # "stack_rndsegfault": lambda: KhaosFaultProblem(KhaosFaultName.stack_rndsegfault),
            # "thrash_swapon": lambda: KhaosFaultProblem(KhaosFaultName.thrash_swapon),
            # "thrash_swapoff": lambda: KhaosFaultProblem(KhaosFaultName.thrash_swapoff),
            # "memleak_munmap": lambda: KhaosFaultProblem(KhaosFaultName.memleak_munmap),
            # "packet_loss_sendto": lambda: KhaosFaultProblem(KhaosFaultName.packet_loss_sendto),
            # "packet_loss_recvfrom": lambda: KhaosFaultProblem(KhaosFaultName.packet_loss_recvfrom),
            # ==================== DIRECT K8S API ====================
            "ingress_misroute": lambda: IngressMisroute(path="/api", correct_service="frontend-service", wrong_service="recommendation-service"),
            "network_policy_block": lambda: NetworkPolicyBlock(faulty_service="payment-service"),
            # ==================== MULTIPLE INDEPENDENT FAILURES ====================
            "social_net_hotel_res_astro_shop_concurrent_failures": lambda: MultipleIndependentFailures(problems=[K8STargetPortMisconfig(faulty_service="user-service"),MongoDBRevokeAuth(faulty_service="mongodb-geo"),WrongServiceSelector(),]),
            # ad hoc:
            "kubelet_crash": KubeletCrash,
            "workload_imbalance": WorkloadImbalance,
            # ==================== K8S OPERATOR MISOPERATION ==================
            "operator_overload_replicas": K8SOperatorOverloadReplicasFault,
            "operator_non_existent_storage": K8SOperatorNonExistentStorageFault,
            "operator_invalid_affinity_toleration": K8SOperatorInvalidAffinityTolerationFault,
            "operator_security_context_fault": K8SOperatorSecurityContextFault,
            "operator_wrong_update_strategy_fault": K8SOperatorWrongUpdateStrategyFault,
        }
# fmt: on
        self.kubectl = KubeCtl()
        self.non_emulated_cluster_problems = []

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
            return [k for k in self.PROBLEM_REGISTRY.keys() if task_type in k]
        if all:
            return list(self.PROBLEM_REGISTRY.keys())

        # by default, only run problems defined in tasklist.yml
        file_dir = Path(__file__).parent.parent
        tasklist_path = file_dir / "tasklist.yml"

        if not tasklist_path.exists():
            # if tasklist.yml does not exist, run all the problems
            return list(self.PROBLEM_REGISTRY.keys())

        with open(tasklist_path, "r") as f:
            tasklist = yaml.safe_load(f)
        return list(tasklist["all"]["problems"].keys())


    def get_problem_count(self, task_type: str = None):
        if task_type:
            return len([k for k in self.PROBLEM_REGISTRY.keys() if task_type in k])
        return len(self.PROBLEM_REGISTRY)
