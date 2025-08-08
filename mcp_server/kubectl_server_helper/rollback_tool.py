import logging
import os
import tempfile
import time
import traceback
from typing import Optional

import yaml
from pydantic.dataclasses import dataclass

from mcp_server.configs.kubectl_tool_cfg import KubectlToolCfg
from mcp_server.kubectl_server_helper.kubectl import KubeCtl
from mcp_server.kubectl_server_helper.utils import cleanup_kubernetes_yaml, parse_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class RollbackCommand:
    command_type: str
    content: str


@dataclass
class RollbackNode:
    action: str
    rollback: list[RollbackCommand]
    cluster_state: str | None = None


class RollbackTool:
    """Tool to rollback the last agent action by popping from the stack."""

    def __init__(self, config: KubectlToolCfg, action_stack):
        self.action_stack = action_stack
        self.config = config

    def _parse_state_source(self, state_source: str) -> str:
        yaml_content = ""
        if os.path.exists(state_source) and os.path.isfile(state_source):
            logger.info(f"Reading cluster state from file: {state_source}")
            try:
                with open(state_source, "r") as f:
                    yaml_content = f.read()
            except Exception as e:
                error_msg = f"Failed to read state file: {e}"
                logger.error(error_msg)
                return error_msg
        else:
            # if state_source is direct YAML content
            yaml_content = state_source

        return yaml_content

    def _restore_cluster_state(self, state_source: str) -> str:
        """Restore cluster state from a saved YAML representation or file path."""
        # Check if state_source is a file path
        yaml_content = self._parse_state_source(state_source)

        # Identify the type of resources
        try:
            resources = list(yaml.safe_load_all(yaml_content))
        except Exception as e:
            logger.error(f"Failed to parse YAML: {e}")
            return self._apply_yaml_directly(yaml_content)

        if len(resources) == 0:
            return "No resources found in state YAML"

        # TODO: rethink if the resources have dependencies
        # we need to apply resources in the correct order
        return self._apply_resources_in_order(resources, yaml_content)

    def _apply_resources_in_order(self, resources, yaml_content):
        """Apply resources in the correct order respecting dependencies."""
        # 1. First identify and apply any CustomResourceDefinitions
        crd_resources = []
        regular_resources = []

        for resource in resources:
            if not isinstance(resource, dict) or "kind" not in resource:
                continue

            if resource["kind"] == "CustomResourceDefinition":
                crd_resources.append(resource)
            else:
                regular_resources.append(resource)

        # Apply CRDs first if any
        if crd_resources:
            logger.info("Applying CustomResourceDefinitions first...")
            crd_yaml = ""
            for crd in crd_resources:
                crd_yaml += yaml.dump(crd) + "\n---\n"

            self._apply_yaml_directly(crd_yaml)
            # Wait for CRDs to be established
            time.sleep(5)

        # TODO: recosider this dependency order
        # 2. Apply resources in dependency order
        # A more advanced implementation would build a dependency graph
        first_tier = ["Namespace", "ConfigMap", "Secret", "ServiceAccount", "Role", "RoleBinding"]
        second_tier = ["Service", "PersistentVolumeClaim", "PersistentVolume"]
        third_tier = ["DaemonSet", "Job", "CronJob"]
        deployment_tier = ["Deployment", "StatefulSet"]

        for tier in [first_tier, second_tier, third_tier, deployment_tier]:
            tier_resources = [r for r in regular_resources if r.get("kind") in tier]
            if tier_resources:
                if tier == deployment_tier:
                    for resource in tier_resources:
                        self._apply_yaml_deployment(resource)

                    if self.config.clear_replicaset:
                        time.sleep(self.config.clear_rs_wait_time)
                        for resource in tier_resources:
                            self._clear_replicasets(resource)
                else:
                    tier_yaml = ""
                    for resource in tier_resources:
                        tier_yaml += yaml.dump(resource) + "\n---\n"

                    if tier_yaml:
                        logger.info(f"Applying {tier} resources...")
                        self._apply_yaml_directly(tier_yaml)

        remaining = [
            r for r in regular_resources if r.get("kind") not in first_tier + second_tier + third_tier + deployment_tier
        ]
        if remaining:
            remaining_yaml = ""
            for resource in remaining:
                remaining_yaml += yaml.dump(resource) + "\n---\n"

            if remaining_yaml:
                logger.info("Applying remaining resources...")
                return self._apply_yaml_directly(remaining_yaml)

        return "Cluster state restored successfully"

    def _apply_yaml_deployment(self, yaml_content):
        # TODO improve this using patch
        strategy = yaml_content.get("spec", {}).get("strategy", {})
        yaml_content["spec"]["strategy"] = {"type": "Recreate"}
        self._apply_yaml_directly(yaml.dump(yaml_content))
        yaml_content["spec"]["strategy"] = strategy
        self._apply_yaml_directly(yaml.dump(yaml_content))

    def _clear_replicasets(self, yaml_content):
        namespace = yaml_content.get("metadata", {}).get("namespace", "")
        matchlabels = yaml_content.get("spec", {}).get("selector", {}).get("matchLabels", {})
        selector = ",".join([f"{k}={v}" for k, v in matchlabels.items()])

        rs_selector = (
            f"kubectl get rs -n {namespace} -l {selector} -o jsonpath="
            "'{.items[?(@.status.replicas==0)].metadata.name}'"
        )

        # Actually we can delete all the replica sets here
        # But here is the reason why we do like this:
        #   For the new replica set, we can just preserve it.
        #   So the 10s we wait is also useful for it being ready.
        #   A corner case is that, the new replica set is scaled to 0.
        #   It will be automatically recreated by K8s, and immediately be ready.

        rs_list = KubeCtl.exec_command(rs_selector)
        if rs_list.returncode == 0:
            rs_list = rs_list.stdout.strip()
            if rs_list == "":
                logger.info("No ReplicaSets to clear.")
                return
            delete_cmd = f"kubectl delete rs {rs_list} -n {namespace}"
            result = KubeCtl.exec_command(delete_cmd)
            if result.returncode == 0:
                logger.info(f"Deleted ReplicaSets: [{rs_list}]")
            else:
                logger.error(f"Failed to delete ReplicaSets [{rs_list}]. Stderr: {result.stderr}")
        else:
            logger.error(f"Failed to get ReplicaSets. Stderr: {rs_list.stderr}")

    def _apply_yaml_directly(self, yaml_content):
        """Helper method to apply YAML directly."""
        if not yaml_content.strip():
            return "No YAML content to apply"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            tmp.write(yaml_content)
            tmp_path = tmp.name

        try:
            restore_cmd = f"kubectl apply -f {tmp_path}"
            result = KubeCtl.exec_command_result(restore_cmd)
            logger.info(f"Applied YAML: {result}")
            return result
        except Exception as e:
            error_msg = f"Failed to apply YAML: {e}"
            logger.error(error_msg)
            return error_msg
        finally:
            try:
                os.remove(tmp_path)
            except Exception as e:
                logger.warning(f"Failed to remove temporary file {tmp_path}: {e}")

    @staticmethod
    def get_namespace_state(namespace: str | None) -> str:
        """Capture the current state of all resources in the cluster."""
        if namespace is None or namespace == "":
            all_namespace_flag = "--all-namespaces"
        else:
            all_namespace_flag = f"-n {namespace}"
        all_resources_command = f"kubectl get all -o yaml {all_namespace_flag}"
        return KubeCtl.exec_command_result(all_resources_command)

    def compare_states(self, current_state: str, previous_state: str) -> str:
        import difflib

        result = difflib.unified_diff(
            previous_state.splitlines(keepends=True),
            current_state.splitlines(keepends=True),
            fromfile="previous_state",
            tofile="current_state",
        )
        return "".join(result)

    def get_previous_rollbackable_cmds(self) -> list[str]:
        return [action.action for action in self.action_stack.stack][::-1]

    def rollback(self) -> str:
        if not hasattr(self, "action_stack") or self.action_stack is None:
            return "Warning: Action Stack disabled. Stop rolling back."

        try:
            if hasattr(self.action_stack, "is_empty") and self.action_stack.is_empty():
                return "No more actions to rollback."
            last_action: Optional[RollbackNode] = self.action_stack.pop()

            if last_action is not None:
                result = []
                for rollback in last_action.rollback:
                    if rollback.command_type == "command":
                        one_step_result = KubeCtl.exec_command(rollback.content)

                        if one_step_result.returncode == 0:
                            output = parse_text(one_step_result.stdout, 1000)
                            result.append(f"Rollback command: {rollback.content}; " f"Execution result: {output}")
                            logger.info(result[-1])
                        else:
                            raise RuntimeError(f"Error executing rollback command: {one_step_result.stderr}")

                    elif rollback.command_type == "file":
                        one_step_result = self._restore_cluster_state(rollback.content)
                        result.append(
                            f"Try to restore cluster state with file {rollback.content}. " f"Result: {one_step_result}"
                        )
                        logger.info(result[-1])
                    else:
                        raise ValueError(f"Unknown rollback type: {rollback.type}")

                rollback_process_desc = (
                    f"Rolled back the previous command: {last_action.action}.\n"
                    f"-------------------Rollback Process:-------------------\n"
                )
                for i, one_step_txt in enumerate(result):
                    rollback_process_desc += f"\nStep {i + 1}:\n{one_step_txt}\n"
                rollback_process_desc += f"-------------------End of Rollback Process:-------------------\n"

                if self.config.validate_rollback:
                    time.sleep(self.config.retry_wait_time)
                    current_state = RollbackTool.get_namespace_state(self.config.namespace)
                    current_state = cleanup_kubernetes_yaml(current_state)
                    last_state = self._parse_state_source(last_action.cluster_state)
                    diff = self.compare_states(current_state, last_state)
                    raw_filename = os.path.basename(last_action.cluster_state).replace("validation_", "")
                    diff_file = os.path.join(
                        self.config.output_dir,
                        "rollback_validation",
                        f"rollback_diff_{raw_filename}",
                    )
                    os.makedirs(os.path.dirname(diff_file), exist_ok=True)
                    with open(diff_file, "w") as f:
                        f.write(diff)
                    ref_file = os.path.join(
                        self.config.output_dir,
                        "rollback_validation",
                        f"rollback_ref_{raw_filename}",
                    )
                    with open(ref_file, "w") as f:
                        f.write(current_state)

                return rollback_process_desc
            return "No more actions to rollback."

        except Exception as e:
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error(f"Error traceback: {tb}")
            return f"Error during rollback: {str(e)}"
