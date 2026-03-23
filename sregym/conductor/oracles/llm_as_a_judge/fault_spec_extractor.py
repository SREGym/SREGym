"""Extract structured fault specifications from Problem objects.

The DiagnosisJudge historically received only `problem.root_cause` — a short
natural-language sentence.  This module introspects the *Problem* instance
(and the injector it wraps) to build a rich, machine-readable fault
specification that a judge LLM can compare against the agent's diagnosis with
much higher precision.

Nothing in this module touches the cluster or network; it only reads Python
attributes that were already set during ``Problem.__init__``.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FaultSpec:
    """Structured representation of a single injected fault."""

    # What was broken
    fault_category: str = ""  # e.g. "misconfiguration", "resource_exhaustion", "auth_failure"
    fault_mechanism: str = ""  # e.g. "incorrect_port", "missing_env_var", "revoked_auth"
    fault_description: str = ""  # Human-readable summary (from root_cause)

    # Where it was broken
    target_component: str = ""  # e.g. "checkout", "frontend", "mongodb-geo"
    target_resource_kind: str = ""  # e.g. "Deployment", "Service", "ConfigMap", "NetworkPolicy"
    namespace: str = ""

    # Injection parameters  (key→value dict of whatever is relevant)
    parameters: dict[str, Any] = field(default_factory=dict)

    # Source information (for the judge to understand provenance)
    injector_class: str = ""
    injector_method: str = ""
    problem_class: str = ""

    def to_prompt_section(self) -> str:
        """Render as a markdown section suitable for the judge prompt."""
        lines = [
            "### Structured Fault Specification",
            "",
            f"**Problem class:** `{self.problem_class}`",
            f"**Fault category:** {self.fault_category}",
            f"**Fault mechanism:** {self.fault_mechanism}",
            f"**Target component:** `{self.target_component}` ({self.target_resource_kind})",
            f"**Namespace:** `{self.namespace}`",
        ]
        if self.parameters:
            lines.append("**Injection parameters:**")
            for k, v in self.parameters.items():
                lines.append(f"  - `{k}`: `{v}`")
        lines.append(f"**Injector:** `{self.injector_class}.{self.injector_method}`")
        lines.append("")
        lines.append("### Ground-Truth Root Cause (natural language)")
        lines.append(self.fault_description)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fault taxonomy  – maps fault_mechanism names to category + default symptoms
# ---------------------------------------------------------------------------

_FAULT_TAXONOMY: dict[str, dict[str, Any]] = {
    # --- Misconfiguration ---
    "incorrect_port": {"category": "misconfiguration"},
    "incorrect_port_assignment": {"category": "misconfiguration"},
    "incorrect_image": {"category": "misconfiguration"},
    "misconfig_app": {"category": "misconfiguration"},
    "missing_env_variable": {"category": "misconfiguration"},
    "env_variable_shadowing": {"category": "misconfiguration"},
    "wrong_service_selector": {"category": "misconfiguration"},
    "wrong_dns_policy": {"category": "misconfiguration"},
    "wrong_bin_usage": {"category": "misconfiguration"},
    "configmap_drift": {"category": "misconfiguration"},
    "missing_configmap": {"category": "misconfiguration"},
    "target_port": {"category": "misconfiguration"},
    "k8s_target_port": {"category": "misconfiguration"},
    "sidecar_port_conflict": {"category": "misconfiguration"},
    "service_port_conflict": {"category": "misconfiguration"},
    "ingress_misroute": {"category": "misconfiguration"},
    "rolling_update_misconfigured": {"category": "misconfiguration"},
    "stale_coredns_config": {"category": "misconfiguration"},
    # --- Scheduling / Resource ---
    "assign_to_non_existent_node": {"category": "scheduling"},
    "taint_no_toleration": {"category": "scheduling"},
    "resource_request": {"category": "resource_exhaustion"},
    "namespace_memory_limit": {"category": "resource_exhaustion"},
    "duplicate_pvc_mounts": {"category": "scheduling"},
    "pvc_claim_mismatch": {"category": "scheduling"},
    "persistent_volume_affinity_violation": {"category": "scheduling"},
    "pod_anti_affinity_deadlock": {"category": "scheduling"},
    "scale_pod": {"category": "resource_exhaustion"},
    "overload_replicas": {"category": "resource_exhaustion"},
    # --- Authentication / Authorization ---
    "revoke_auth": {"category": "auth_failure"},
    "storage_user_unregistered": {"category": "auth_failure"},
    "auth_miss_mongodb": {"category": "auth_failure"},
    "valkey_auth_disruption": {"category": "auth_failure"},
    "rbac_misconfiguration": {"category": "auth_failure"},
    # --- Network ---
    "network_policy_block": {"category": "network"},
    "service_dns_resolution_failure": {"category": "network"},
    # --- Feature flag / Application logic ---
    "feature_flag": {"category": "feature_flag"},
    "manual_gc": {"category": "resource_exhaustion"},
    # --- Metastable ---
    "rpc_retry_storm": {"category": "metastable_failure"},
    "gc_capacity_degradation": {"category": "metastable_failure"},
    # --- Memory / Storage ---
    "valkey_memory_disruption": {"category": "resource_exhaustion"},
    # --- Operator misoperation ---
    "invalid_affinity_toleration": {"category": "scheduling"},
    "non_existent_storage": {"category": "scheduling"},
    "security_context_fault": {"category": "misconfiguration"},
    "wrong_update_strategy": {"category": "misconfiguration"},
    "wrong_operator_image": {"category": "misconfiguration"},
    # --- Hardware / Kernel (Khaos) ---
    "kubelet_crash": {"category": "infrastructure"},
    "workload_imbalance": {"category": "infrastructure"},
}


# ---------------------------------------------------------------------------
# Extractor logic
# ---------------------------------------------------------------------------


def _infer_fault_mechanism(problem) -> str:
    """Infer the fault mechanism name from the problem class and its attributes."""
    cls_name = type(problem).__name__

    # Direct mapping from well-known class names
    _CLASS_TO_MECHANISM = {
        "IncorrectPortAssignment": "incorrect_port_assignment",
        "IncorrectImage": "incorrect_image",
        "MissingEnvVariable": "missing_env_variable",
        "MisconfigAppHotelRes": "misconfig_app",
        "MongoDBRevokeAuth": "revoke_auth",
        "MongoDBAuthMissing": "auth_miss_mongodb",
        "MongoDBUserUnregistered": "storage_user_unregistered",
        "ValkeyAuthDisruption": "valkey_auth_disruption",
        "ValkeyMemoryDisruption": "valkey_memory_disruption",
        "AssignNonExistentNode": "assign_to_non_existent_node",
        "ConfigMapDrift": "configmap_drift",
        "DuplicatePVCMounts": "duplicate_pvc_mounts",
        "EnvVariableShadowing": "env_variable_shadowing",
        "LivenessProbeMisconfiguration": "liveness_probe_misconfiguration",
        "LivenessProbeTooAggressive": "liveness_probe_too_aggressive",
        "ReadinessProbeMisconfiguration": "readiness_probe_misconfiguration",
        "FailedReadinessProbe": "failed_readiness_probe",
        "WrongServiceSelector": "wrong_service_selector",
        "WrongDNSPolicy": "wrong_dns_policy",
        "WrongBinUsage": "wrong_bin_usage",
        "MissingConfigMap": "missing_configmap",
        "MissingService": "missing_service",
        "ServiceDNSResolutionFailure": "service_dns_resolution_failure",
        "StaleCoreDNSConfig": "stale_coredns_config",
        "SidecarPortConflict": "sidecar_port_conflict",
        "ServicePortConflict": "service_port_conflict",
        "IngressMisroute": "ingress_misroute",
        "NetworkPolicyBlock": "network_policy_block",
        "TaintNoToleration": "taint_no_toleration",
        "RollingUpdateMisconfigured": "rolling_update_misconfigured",
        "PVCClaimMismatch": "pvc_claim_mismatch",
        "PersistentVolumeAffinityViolation": "persistent_volume_affinity_violation",
        "PodAntiAffinityDeadlock": "pod_anti_affinity_deadlock",
        "NamespaceMemoryLimit": "namespace_memory_limit",
        "RBACMisconfiguration": "rbac_misconfiguration",
        "ResourceRequestTooLarge": "resource_request",
        "ResourceRequestTooSmall": "resource_request",
        "CapacityDecreaseRPCRetryStorm": "rpc_retry_storm",
        "LoadSpikeRPCRetryStorm": "rpc_retry_storm",
        "GCCapacityDegradation": "gc_capacity_degradation",
        "KubeletCrash": "kubelet_crash",
        "WorkloadImbalance": "workload_imbalance",
        "ScalePodSocialNet": "scale_pod",
        "K8STargetPortMisconfig": "target_port",
        "ProductCatalogServiceFailure": "feature_flag",
        "PaymentServiceFailure": "feature_flag",
        "PaymentServiceUnreachable": "feature_flag",
        "CartServiceFailure": "feature_flag",
        "AdServiceFailure": "feature_flag",
        "AdServiceHighCpu": "manual_gc",
        "AdServiceManualGc": "manual_gc",
        "FaultyImageCorrelated": "incorrect_image",
        "UpdateIncompatibleCorrelated": "incorrect_image",
        "ImageSlowLoad": "incorrect_image",
        "SilentDataCorruption": "misconfig_app",
        "K8SOperatorOverloadReplicasFault": "overload_replicas",
        "K8SOperatorNonExistentStorageFault": "non_existent_storage",
        "K8SOperatorInvalidAffinityTolerationFault": "invalid_affinity_toleration",
        "K8SOperatorSecurityContextFault": "security_context_fault",
        "K8SOperatorWrongUpdateStrategyFault": "wrong_update_strategy",
        "K8SOperatorWrongOperatorImage": "wrong_operator_image",
        "LoadGeneratorFloodHomepage": "workload_imbalance",
    }

    if cls_name in _CLASS_TO_MECHANISM:
        return _CLASS_TO_MECHANISM[cls_name]

    # Fallback: convert CamelCase class name to snake_case
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls_name).lower()


def _infer_target_resource_kind(problem) -> str:
    """Guess the Kubernetes resource kind from problem attributes."""
    root = getattr(problem, "root_cause", "") or ""
    lower = root.lower()

    if "networkpolicy" in lower or "network policy" in lower:
        return "NetworkPolicy"
    if "configmap" in lower:
        return "ConfigMap"
    if "ingress" in lower:
        return "Ingress"
    if "service" in lower and "deployment" not in lower:
        return "Service"
    if "daemonset" in lower:
        return "DaemonSet"
    if "statefulset" in lower:
        return "StatefulSet"
    if "pvc" in lower or "persistentvolumeclaim" in lower:
        return "PersistentVolumeClaim"

    # Default: most problems target a Deployment
    return "Deployment"


def _extract_injection_parameters(problem) -> dict[str, Any]:
    """Pull out any injector-specific parameters the problem stores."""
    params: dict[str, Any] = {}
    # Common attributes set by various Problem subclasses
    for attr in (
        "env_var",
        "env_var_value",
        "incorrect_port",
        "correct_port",
        "bad_image",
        "correct_image",
        "policy_name",
        "path",
        "correct_service",
        "wrong_service",
        "ingress_name",
        "memory_limit",
    ):
        val = getattr(problem, attr, None)
        if val is not None:
            params[attr] = val

    # Check for injector instances and pull their namespace
    injector = getattr(problem, "injector", None)
    if injector is not None:
        params["injector_namespace"] = getattr(injector, "namespace", "")

    return params


def _infer_injector_info(problem) -> tuple[str, str]:
    """Return (injector_class_name, inject_method_name) by inspecting inject_fault."""
    injector = getattr(problem, "injector", None)
    injector_cls = type(injector).__name__ if injector else ""

    # Try to figure out which inject method is called
    try:
        src = inspect.getsource(type(problem).inject_fault)
        # Look for calls like  self.injector.inject_<method>(  or  injector.inject_<method>(
        m = re.search(r"\.inject_(\w+)\(", src)
        if m:
            return injector_cls, f"inject_{m.group(1)}"
    except (OSError, TypeError):
        pass

    return injector_cls, "inject_fault"


def extract_fault_spec(problem) -> FaultSpec:
    """Build a FaultSpec from a live Problem instance.

    This is the main entry point.  Call it like::

        spec = extract_fault_spec(problem)
        prompt_text = spec.to_prompt_section()
    """
    mechanism = _infer_fault_mechanism(problem)
    taxonomy = _FAULT_TAXONOMY.get(mechanism, {})

    injector_cls, inject_method = _infer_injector_info(problem)

    return FaultSpec(
        fault_category=taxonomy.get("category", "unknown"),
        fault_mechanism=mechanism,
        fault_description=getattr(problem, "root_cause", "") or "",
        target_component=getattr(problem, "faulty_service", "") or "",
        target_resource_kind=_infer_target_resource_kind(problem),
        namespace=getattr(problem, "namespace", "") or "",
        parameters=_extract_injection_parameters(problem),
        injector_class=injector_cls,
        injector_method=inject_method,
        problem_class=type(problem).__name__,
    )


def extract_fault_spec_dict(problem) -> dict[str, Any]:
    """Convenience wrapper that returns a plain dict."""
    return asdict(extract_fault_spec(problem))
