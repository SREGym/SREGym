"""Registered environment-scaled faults owned by this application."""

from collections.abc import Callable

from sregym.conductor.problems.apps.saleor.admission import AdmissionWebhookOutageIA
from sregym.conductor.problems.apps.saleor.checkout import (
    CatalogUnpublishedSaleor,
    ChannelDeactivatedSaleor,
    PaymentGatewayDisabledSaleor,
    SchemaColumnDriftSaleor,
)
from sregym.conductor.problems.apps.saleor.configuration import (
    EnvVariableShadowingIA,
    RBACMisconfigurationSaleor,
    SecretRotationStaleEnvCredentialsIA,
    SidecarPortConflictSaleor,
    WrongBinUsageSaleor,
    WrongDNSPolicyIA,
)
from sregym.conductor.problems.apps.saleor.database import (
    IntegerOverflowPrimaryKeySaleor,
    PostgresLockContentionSaleor,
    RevokeAuthWorkerRoleSaleor,
    StorageUserUnregisteredWorkerRoleSaleor,
)
from sregym.conductor.problems.apps.saleor.helpers import _ORIGINAL_APPS
from sregym.conductor.problems.apps.saleor.readiness import LivenessProbeTooAggressiveSaleor
from sregym.conductor.problems.apps.saleor.resources import (
    FileDescriptorExhaustionSaleor,
    NamespaceMemoryLimitIA,
    NightlyRebalanceOOMSaleor,
    PriorityPreemptionCascadeSaleor,
    ResourceRequestTooSmallSaleor,
)
from sregym.conductor.problems.apps.saleor.scheduling import AssignNonExistentNodeSaleor, PVCClaimMismatchSaleor
from sregym.conductor.problems.apps.saleor.storage import LatentSectorErrorSaleor
from sregym.conductor.problems.base import Problem

SOURCE_PORTS: dict[str, tuple[str, Callable[[], Problem]]] = {
    **{
        f"liveness_probe_too_aggressive_{app}": (
            "liveness_probe_too_aggressive_saleor",
            LivenessProbeTooAggressiveSaleor,
        )
        for app in _ORIGINAL_APPS
    },
    **{
        f"sidecar_port_conflict_{app}": ("sidecar_port_conflict_saleor", SidecarPortConflictSaleor)
        for app in _ORIGINAL_APPS
    },
    "assign_to_non_existent_node": ("assign_to_non_existent_node_saleor", AssignNonExistentNodeSaleor),
    "wrong_bin_usage": ("wrong_bin_usage_saleor", WrongBinUsageSaleor),
    "priority_preemption_cascade_hotel_reservation": (
        "priority_preemption_cascade_saleor",
        PriorityPreemptionCascadeSaleor,
    ),
    "pvc_claim_mismatch": ("pvc_claim_mismatch_saleor", PVCClaimMismatchSaleor),
    "rbac_misconfiguration": ("rbac_misconfiguration_saleor", RBACMisconfigurationSaleor),
    "resource_request_too_small": ("resource_request_too_small_saleor", ResourceRequestTooSmallSaleor),
    "nightly_rebalance_oom_hotel_reservation": ("nightly_rebalance_oom_saleor", NightlyRebalanceOOMSaleor),
    "astronomy_shop_payment_service_failure": (
        "astronomy_shop_payment_service_failure_saleor",
        PaymentGatewayDisabledSaleor,
    ),
    "astronomy_shop_cart_service_failure": ("astronomy_shop_cart_service_failure_saleor", ChannelDeactivatedSaleor),
    "astronomy_shop_product_catalog_service_failure": (
        "astronomy_shop_product_catalog_service_failure_saleor",
        CatalogUnpublishedSaleor,
    ),
    "trainticket_f22_sql_column_name_mismatch_error": (
        "trainticket_f22_sql_column_name_mismatch_error_saleor",
        SchemaColumnDriftSaleor,
    ),
    "integer_overflow_primary_key_astronomy_shop": (
        "integer_overflow_primary_key_saleor",
        IntegerOverflowPrimaryKeySaleor,
    ),
    "postgres_lock_contention_product_catalog": ("postgres_lock_contention_saleor", PostgresLockContentionSaleor),
    "file_descriptor_exhaustion": ("file_descriptor_exhaustion_saleor", FileDescriptorExhaustionSaleor),
    "latent_sector_error": ("latent_sector_error_saleor", LatentSectorErrorSaleor),
    "revoke_auth_mongodb-2": ("revoke_auth_saleor", RevokeAuthWorkerRoleSaleor),
    "storage_user_unregistered-2": ("storage_user_unregistered_saleor", StorageUserUnregisteredWorkerRoleSaleor),
    "env_variable_shadowing_astronomy_shop": ("env_variable_shadowing_saleor", EnvVariableShadowingIA),
    "admission_webhook_outage_hotel_reservation": ("admission_webhook_outage_saleor", AdmissionWebhookOutageIA),
    "wrong_dns_policy_astronomy_shop": ("wrong_dns_policy_saleor", WrongDNSPolicyIA),
    "namespace_memory_limit": ("namespace_memory_limit_saleor", NamespaceMemoryLimitIA),
    "secret_rotation_stale_env_credentials_astronomy_shop": (
        "secret_rotation_stale_env_credentials_saleor",
        SecretRotationStaleEnvCredentialsIA,
    ),
}
