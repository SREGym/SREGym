"""Cluster state management for resetting cluster between benchmark problems."""

import hashlib
import json
import logging
from dataclasses import dataclass, field

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.infra.cluster_state")
logger.propagate = True
logger.setLevel(logging.DEBUG)


# Namespaces that should never be deleted during reconciliation
PROTECTED_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "default",
        # Infrastructure namespaces managed by the benchmark
        "openebs",
        "observe",  # Prometheus namespace
        "khaos",
    }
)


@dataclass
class ClusterBaseline:
    """Snapshot of cluster state to reconcile back to."""

    namespaces: set[str] = field(default_factory=set)
    cluster_roles: set[str] = field(default_factory=set)
    cluster_role_bindings: set[str] = field(default_factory=set)
    persistent_volumes: set[str] = field(default_factory=set)
    storage_classes: set[str] = field(default_factory=set)
    crds: set[str] = field(default_factory=set)
    validating_webhook_configs: set[str] = field(default_factory=set)
    mutating_webhook_configs: set[str] = field(default_factory=set)
    node_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    node_taints: dict[str, list] = field(default_factory=dict)
    coredns_configmap_data: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize baseline to a dictionary for logging/debugging."""
        return {
            "namespaces": sorted(self.namespaces),
            "cluster_roles": sorted(self.cluster_roles),
            "cluster_role_bindings": sorted(self.cluster_role_bindings),
            "persistent_volumes": sorted(self.persistent_volumes),
            "storage_classes": sorted(self.storage_classes),
            "crds": sorted(self.crds),
            "validating_webhook_configs": sorted(self.validating_webhook_configs),
            "mutating_webhook_configs": sorted(self.mutating_webhook_configs),
            "node_labels": self.node_labels,
            "node_taints": self.node_taints,
            "coredns_configmap_hash": hashlib.md5(
                json.dumps(self.coredns_configmap_data, sort_keys=True).encode()
            ).hexdigest(),
        }


class ClusterStateManager:
    """Manages cluster state snapshots and reconciliation for benchmark isolation."""

    def __init__(self, kubectl: KubeCtl):
        self.kubectl = kubectl
        self.baseline: ClusterBaseline | None = None

        # Initialize Kubernetes API clients
        self.core_v1 = client.CoreV1Api()
        self.rbac_v1 = client.RbacAuthorizationV1Api()
        self.storage_v1 = client.StorageV1Api()
        self.apiextensions_v1 = client.ApiextensionsV1Api()
        self.admission_v1 = client.AdmissionregistrationV1Api()

    def capture_baseline(self) -> ClusterBaseline:
        """
        Capture current cluster state as the baseline.
        Should be called after infrastructure is deployed but before any problems run.
        """
        logger.info("Capturing cluster baseline state...")

        self.baseline = ClusterBaseline(
            namespaces=self._get_namespaces(),
            cluster_roles=self._get_cluster_roles(),
            cluster_role_bindings=self._get_cluster_role_bindings(),
            persistent_volumes=self._get_persistent_volumes(),
            storage_classes=self._get_storage_classes(),
            crds=self._get_crds(),
            validating_webhook_configs=self._get_validating_webhook_configs(),
            mutating_webhook_configs=self._get_mutating_webhook_configs(),
            node_labels=self._get_node_labels(),
            node_taints=self._get_node_taints(),
            coredns_configmap_data=self._get_coredns_configmap_data(),
        )

        logger.info(f"Baseline captured: {self.baseline.to_dict()}")
        return self.baseline

    def reconcile_to_baseline(self) -> dict:
        """
        Reset cluster to baseline state.
        Returns a summary of changes made.
        """
        if self.baseline is None:
            logger.warning("No baseline captured. Skipping reconciliation.")
            return {"skipped": True, "reason": "no_baseline"}

        logger.info("Reconciling cluster to baseline state...")
        changes = {
            "namespaces_deleted": [],
            "cluster_roles_deleted": [],
            "cluster_role_bindings_deleted": [],
            "persistent_volumes_deleted": [],
            "storage_classes_deleted": [],
            "crds_deleted": [],
            "validating_webhook_configs_deleted": [],
            "mutating_webhook_configs_deleted": [],
            "nodes_labels_reset": [],
            "nodes_taints_reset": [],
            "coredns_reset": False,
        }

        # 1. Delete unexpected namespaces
        current_namespaces = self._get_namespaces()
        unexpected_namespaces = current_namespaces - self.baseline.namespaces - PROTECTED_NAMESPACES
        for ns in unexpected_namespaces:
            logger.info(f"Deleting unexpected namespace: {ns}")
            try:
                self.kubectl.delete_namespace(ns)
                changes["namespaces_deleted"].append(ns)
            except Exception as e:
                logger.warning(f"Failed to delete namespace {ns}: {e}")

        # 2. Delete unexpected ClusterRoles
        current_cluster_roles = self._get_cluster_roles()
        unexpected_roles = current_cluster_roles - self.baseline.cluster_roles
        for role in unexpected_roles:
            # Skip system roles that may have been auto-created
            if role.startswith("system:") or role.startswith("kubeadm:"):
                continue
            logger.info(f"Deleting unexpected ClusterRole: {role}")
            try:
                self.rbac_v1.delete_cluster_role(name=role)
                changes["cluster_roles_deleted"].append(role)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete ClusterRole {role}: {e}")

        # 3. Delete unexpected ClusterRoleBindings
        current_bindings = self._get_cluster_role_bindings()
        unexpected_bindings = current_bindings - self.baseline.cluster_role_bindings
        for binding in unexpected_bindings:
            if binding.startswith("system:") or binding.startswith("kubeadm:"):
                continue
            logger.info(f"Deleting unexpected ClusterRoleBinding: {binding}")
            try:
                self.rbac_v1.delete_cluster_role_binding(name=binding)
                changes["cluster_role_bindings_deleted"].append(binding)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete ClusterRoleBinding {binding}: {e}")

        # 4. Delete unexpected PersistentVolumes
        current_pvs = self._get_persistent_volumes()
        unexpected_pvs = current_pvs - self.baseline.persistent_volumes
        for pv in unexpected_pvs:
            logger.info(f"Deleting unexpected PersistentVolume: {pv}")
            try:
                self.core_v1.delete_persistent_volume(name=pv)
                changes["persistent_volumes_deleted"].append(pv)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete PersistentVolume {pv}: {e}")

        # 5. Delete unexpected StorageClasses
        current_scs = self._get_storage_classes()
        unexpected_scs = current_scs - self.baseline.storage_classes
        for sc in unexpected_scs:
            logger.info(f"Deleting unexpected StorageClass: {sc}")
            try:
                self.storage_v1.delete_storage_class(name=sc)
                changes["storage_classes_deleted"].append(sc)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete StorageClass {sc}: {e}")

        # 6. Delete unexpected CRDs
        current_crds = self._get_crds()
        unexpected_crds = current_crds - self.baseline.crds
        for crd in unexpected_crds:
            logger.info(f"Deleting unexpected CRD: {crd}")
            try:
                self.apiextensions_v1.delete_custom_resource_definition(name=crd)
                changes["crds_deleted"].append(crd)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete CRD {crd}: {e}")

        # 7. Delete unexpected ValidatingWebhookConfigurations
        current_vwc = self._get_validating_webhook_configs()
        unexpected_vwc = current_vwc - self.baseline.validating_webhook_configs
        for vwc in unexpected_vwc:
            logger.info(f"Deleting unexpected ValidatingWebhookConfiguration: {vwc}")
            try:
                self.admission_v1.delete_validating_webhook_configuration(name=vwc)
                changes["validating_webhook_configs_deleted"].append(vwc)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete ValidatingWebhookConfiguration {vwc}: {e}")

        # 8. Delete unexpected MutatingWebhookConfigurations
        current_mwc = self._get_mutating_webhook_configs()
        unexpected_mwc = current_mwc - self.baseline.mutating_webhook_configs
        for mwc in unexpected_mwc:
            logger.info(f"Deleting unexpected MutatingWebhookConfiguration: {mwc}")
            try:
                self.admission_v1.delete_mutating_webhook_configuration(name=mwc)
                changes["mutating_webhook_configs_deleted"].append(mwc)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete MutatingWebhookConfiguration {mwc}: {e}")

        # 9. Reset node labels
        changes["nodes_labels_reset"] = self._reconcile_node_labels()

        # 10. Reset node taints
        changes["nodes_taints_reset"] = self._reconcile_node_taints()

        # 11. Reset CoreDNS ConfigMap if modified
        if self._is_coredns_modified():
            logger.info("Resetting CoreDNS ConfigMap to baseline")
            self._restore_coredns_configmap()
            changes["coredns_reset"] = True

        logger.info(f"Reconciliation complete: {changes}")
        return changes

    def _get_namespaces(self) -> set[str]:
        """Get all namespace names in the cluster."""
        try:
            ns_list = self.core_v1.list_namespace()
            return {ns.metadata.name for ns in ns_list.items}
        except ApiException as e:
            logger.error(f"Failed to list namespaces: {e}")
            return set()

    def _get_cluster_roles(self) -> set[str]:
        """Get all ClusterRole names."""
        try:
            roles = self.rbac_v1.list_cluster_role()
            return {role.metadata.name for role in roles.items}
        except ApiException as e:
            logger.error(f"Failed to list ClusterRoles: {e}")
            return set()

    def _get_cluster_role_bindings(self) -> set[str]:
        """Get all ClusterRoleBinding names."""
        try:
            bindings = self.rbac_v1.list_cluster_role_binding()
            return {binding.metadata.name for binding in bindings.items}
        except ApiException as e:
            logger.error(f"Failed to list ClusterRoleBindings: {e}")
            return set()

    def _get_persistent_volumes(self) -> set[str]:
        """Get all PersistentVolume names."""
        try:
            pvs = self.core_v1.list_persistent_volume()
            return {pv.metadata.name for pv in pvs.items}
        except ApiException as e:
            logger.error(f"Failed to list PersistentVolumes: {e}")
            return set()

    def _get_storage_classes(self) -> set[str]:
        """Get all StorageClass names."""
        try:
            scs = self.storage_v1.list_storage_class()
            return {sc.metadata.name for sc in scs.items}
        except ApiException as e:
            logger.error(f"Failed to list StorageClasses: {e}")
            return set()

    def _get_crds(self) -> set[str]:
        """Get all CustomResourceDefinition names."""
        try:
            crds = self.apiextensions_v1.list_custom_resource_definition()
            return {crd.metadata.name for crd in crds.items}
        except ApiException as e:
            logger.error(f"Failed to list CRDs: {e}")
            return set()

    def _get_validating_webhook_configs(self) -> set[str]:
        """Get all ValidatingWebhookConfiguration names."""
        try:
            configs = self.admission_v1.list_validating_webhook_configuration()
            return {cfg.metadata.name for cfg in configs.items}
        except ApiException as e:
            logger.error(f"Failed to list ValidatingWebhookConfigurations: {e}")
            return set()

    def _get_mutating_webhook_configs(self) -> set[str]:
        """Get all MutatingWebhookConfiguration names."""
        try:
            configs = self.admission_v1.list_mutating_webhook_configuration()
            return {cfg.metadata.name for cfg in configs.items}
        except ApiException as e:
            logger.error(f"Failed to list MutatingWebhookConfigurations: {e}")
            return set()

    def _get_node_labels(self) -> dict[str, dict[str, str]]:
        """Get labels for all nodes."""
        try:
            nodes = self.core_v1.list_node()
            return {node.metadata.name: dict(node.metadata.labels or {}) for node in nodes.items}
        except ApiException as e:
            logger.error(f"Failed to get node labels: {e}")
            return {}

    def _get_node_taints(self) -> dict[str, list]:
        """Get taints for all nodes."""
        try:
            nodes = self.core_v1.list_node()
            result = {}
            for node in nodes.items:
                taints = node.spec.taints or []
                # Convert taint objects to dicts for comparison
                result[node.metadata.name] = [{"key": t.key, "value": t.value, "effect": t.effect} for t in taints]
            return result
        except ApiException as e:
            logger.error(f"Failed to get node taints: {e}")
            return {}

    def _get_coredns_configmap_data(self) -> dict[str, str]:
        """Get CoreDNS ConfigMap data."""
        try:
            cm = self.core_v1.read_namespaced_config_map(name="coredns", namespace="kube-system")
            return dict(cm.data or {})  # type: ignore[union-attr]
        except ApiException as e:
            if e.status == 404:
                logger.warning("CoreDNS ConfigMap not found")
                return {}
            logger.error(f"Failed to get CoreDNS ConfigMap: {e}")
            return {}

    def _is_coredns_modified(self) -> bool:
        """Check if CoreDNS ConfigMap has been modified from baseline."""
        if not self.baseline:
            return False
        current_data = self._get_coredns_configmap_data()
        return current_data != self.baseline.coredns_configmap_data

    def _restore_coredns_configmap(self):
        """Restore CoreDNS ConfigMap to baseline state."""
        if not self.baseline or not self.baseline.coredns_configmap_data:
            logger.warning("No baseline CoreDNS data to restore")
            return

        try:
            cm = self.core_v1.read_namespaced_config_map(name="coredns", namespace="kube-system")
            cm.data = self.baseline.coredns_configmap_data  # type: ignore[union-attr]
            self.core_v1.replace_namespaced_config_map(name="coredns", namespace="kube-system", body=cm)
            # Restart CoreDNS pods to pick up the change
            self.kubectl.exec_command(
                "kubectl rollout restart deployment coredns -n kube-system 2>/dev/null || "
                "kubectl rollout restart daemonset coredns -n kube-system 2>/dev/null || true"
            )
            logger.info("CoreDNS ConfigMap restored to baseline")
        except ApiException as e:
            logger.error(f"Failed to restore CoreDNS ConfigMap: {e}")

    def _reconcile_node_labels(self) -> list:
        """
        Reconcile node labels to baseline state.
        Returns list of nodes that were modified.
        """
        if not self.baseline:
            return []

        modified_nodes = []
        current_labels = self._get_node_labels()

        for node_name, baseline_labels in self.baseline.node_labels.items():
            if node_name not in current_labels:
                continue  # Node no longer exists

            current = current_labels[node_name]

            # Find labels to remove (present now but not in baseline)
            # Exclude kubernetes.io labels as they're managed by the system
            labels_to_remove = {
                k
                for k in current.keys() - baseline_labels.keys()
                if not k.startswith("kubernetes.io/")
                and not k.startswith("node.kubernetes.io/")
                and not k.startswith("node-role.kubernetes.io/")
            }

            # Find labels to restore (different value or missing)
            labels_to_restore = {k: v for k, v in baseline_labels.items() if k not in current or current[k] != v}

            if labels_to_remove or labels_to_restore:
                try:
                    # Build patch body
                    patch_labels = {}
                    for label in labels_to_remove:
                        patch_labels[label] = None  # None removes the label
                    for k, v in labels_to_restore.items():
                        patch_labels[k] = v

                    body = {"metadata": {"labels": patch_labels}}
                    self.core_v1.patch_node(name=node_name, body=body)
                    modified_nodes.append(node_name)
                    logger.info(
                        f"Reset labels on node {node_name}: "
                        f"removed={labels_to_remove}, restored={list(labels_to_restore.keys())}"
                    )
                except ApiException as e:
                    logger.warning(f"Failed to reconcile labels for node {node_name}: {e}")

        return modified_nodes

    def _reconcile_node_taints(self) -> list:
        """
        Reconcile node taints to baseline state.
        Returns list of nodes that were modified.
        """
        if not self.baseline:
            return []

        modified_nodes = []
        current_taints = self._get_node_taints()

        for node_name, baseline_taints in self.baseline.node_taints.items():
            if node_name not in current_taints:
                continue  # Node no longer exists

            current = current_taints[node_name]

            # Compare taint lists (order-independent)
            baseline_set = {json.dumps(t, sort_keys=True) for t in baseline_taints}
            current_set = {json.dumps(t, sort_keys=True) for t in current}

            if baseline_set != current_set:
                try:
                    # Reconstruct taints from baseline
                    taints = [
                        client.V1Taint(key=t["key"], value=t.get("value"), effect=t["effect"]) for t in baseline_taints
                    ]
                    body = {"spec": {"taints": taints if taints else None}}
                    self.core_v1.patch_node(name=node_name, body=body)
                    modified_nodes.append(node_name)
                    logger.info(f"Reset taints on node {node_name}")
                except ApiException as e:
                    logger.warning(f"Failed to reconcile taints for node {node_name}: {e}")

        return modified_nodes
