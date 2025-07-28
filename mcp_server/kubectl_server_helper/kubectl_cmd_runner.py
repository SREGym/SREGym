import hashlib
import logging
import os
import time
import bashlex
from mcp_server.kubectl_server_helper.kubectl import KubeCtl, DryRunResult, DryRunStatus

from mcp_server.configs.kubectl_tool_cfg import KubectlToolCfg
from mcp_server.kubectl_server_helper.utils import cleanup_kubernetes_yaml, parse_text
from mcp_server.kubectl_server_helper.rollback_tool import RollbackCommand, RollbackNode, RollbackTool
from mcp_server.kubectl_server_helper.cmd_category import \
    kubectl_safe_commands, \
    kubectl_unsupported_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class KubectlCmdRunner:
    def __init__(self, config: KubectlToolCfg, action_stack=None):
        self.action_stack = action_stack
        self.config = config

    def exec_kubectl_cmd_safely(self, command: str) -> str:
        try:
            if not command.strip().startswith("kubectl"):
                return "Command Rejected: Only kubectl commands are allowed. Please check the command and try again."

            self._check_kubectl_command(command)

            dry_run_result = KubeCtl.dry_run_json_output(command)

            if self.config.forbid_unsafe_commands and not self._is_kubectl_command_safe(command):
                return "Command Rejected: Unsafe command detected. Please check the command and try again."

            logger.info(f"Dry-run result: {dry_run_result.status}, description: {dry_run_result.description}")

            if dry_run_result.status == DryRunStatus.NOEFFECT:
                result = self._execute_kubectl_command(command)
            elif dry_run_result.status == DryRunStatus.ERROR:
                result = dry_run_result.description

                if self.config.verify_dry_run and "Interactive command" not in dry_run_result.description:
                    # Warning: This is only for testing purposes. It may execute malicious commands.
                    exception_triggered = False
                    try:
                        self._execute_kubectl_command(command)
                    except Exception as _:  # noqa F841
                        exception_triggered = True

                    if not exception_triggered:
                        logger.error("Dry-run verification failed (ERROR case)")

                return result
            elif dry_run_result.status == DryRunStatus.SUCCESS:
                if self.config.use_rollback_stack:
                    rollback_command = self._gen_rollback_commands(command, dry_run_result)

                if self.config.verify_dry_run:
                    try:
                        result = self._execute_kubectl_command(command)
                    except Exception as e:
                        logger.error(f"Dry-run verification failed (SUCCESS case): {e}")
                        raise e
                else:
                    result = self._execute_kubectl_command(command)

                if self.config.use_rollback_stack:
                    self.action_stack.push(rollback_command)
            else:
                raise ValueError(f"Unknown dry run status: {dry_run_result.status}")
            return parse_text(result)
        except Exception as exc:
            logger.error(f"Command Rejected: {exc}")
            return f"Command Rejected: {exc}"

    def _check_kubectl_command(self, command: str) -> None:
        # Check interactive subcommands
        for c in kubectl_unsupported_commands:
            if command.startswith(c):
                raise ValueError(f"Interactive command {c} detected. Such commands are not supported.")

        tokens = bashlex.parse(command)
        has_redirection = False

        def traverse_AST(node):
            if node.kind not in ["command", "heredoc", "redirect", "tilde", "word"]:
                if "pipe" in node.kind:
                    raise ValueError("Pipe commands are forbidden")
                raise ValueError(f"Unsupported operator kind: {node.kind}")

            if node.kind == "redirect":
                if ">" in node.type:
                    raise ValueError("Write redirection is forbidden.")
                nonlocal has_redirection
                if "<" in node.type:
                    has_redirection = True

            parts = 1 if node.kind == "command" else 0
            if hasattr(node, "parts"):
                parts += sum(traverse_AST(part) for part in node.parts)

            if parts > 1:
                raise ValueError("Compound commands are forbidden.")

            return parts

        # Check unsupported operators
        for part in tokens:
            traverse_AST(part)

        # Check interactive flags
        parts = list(bashlex.split(command))
        for i, part in enumerate(parts):
            if part in ["--interactive", "-i", "--tty", "-t", "--stdin", "-it"]:
                raise ValueError(f"Interactive flag detected: {part}. Such commands are not supported. "
                                 f"Try to use the command non-interactively.")

            if part in ["-f", "--filename"] and i + 1 < len(parts) and parts[i + 1] == "-":
                if not has_redirection:
                    raise ValueError("Stdin redirected but no input file provided.")

            if part == "--":
                break

    def _is_kubectl_command_safe(self, command: str) -> bool:
        for c in kubectl_safe_commands:
            if command.startswith(c):
                return True
        return False

    def _execute_kubectl_command(self, command: str):
        logger.info(f"Executing command: {command}")
        result = KubeCtl.exec_command(command)
        if result.returncode == 0:
            output = parse_text(result.stdout, 1000)
            logger.info(f"Kubectl MCP Tool command execution:\n{output}")
            return result.stdout
        else:
            logger.error(f"Error executing kubectl command:\n{result.stderr}")
            raise RuntimeError(f"Error executing kubectl command:\n{result.stderr}")

    def _gen_rollback_commands(self, command: str, dry_run_result: DryRunResult) -> RollbackNode:
        """Generate rollback commands based on the dry-run result."""

        # We should return this before execution, since kubectl delete will remove the resource
        return_value = None
        full_state_file = None  # For rollback validation

        state_dir = os.path.join(self.config.output_dir, "kubectl_states")
        os.makedirs(state_dir, exist_ok=True)

        timestamp = int(time.time())
        cmd_hash = hashlib.md5(command.encode(), usedforsecurity=False).hexdigest()[:8]
        state_file = os.path.join(state_dir, f"state_{timestamp}_{cmd_hash}.yaml")

        """ Get the rollback information """
        dry_run_stdout = dry_run_result.result[0]

        namespace = KubeCtl.extract_namespace_from_command(command)
        if namespace is None:
            # Although should be "default"
            namespace = self.config.namespace

        # namespace flag + content
        nsp_flag_ctnt = f"-n {namespace}" if namespace else ""

        rollback_commands = []

        if "created (server dry run)" in dry_run_stdout or "exposed (server dry run)" in dry_run_stdout:
            result = KubeCtl.dry_run_json_output(command, "name")
            rollback_commands = [
                RollbackCommand(
                    "command",
                    "kubectl delete {resource_type} {resource_name} {nsp_flag_ctnt}".format(
                        resource_type=result.result[0],
                        resource_name=result.result[1],
                        nsp_flag_ctnt=nsp_flag_ctnt,
                    ),
                )
            ]
        elif "deleted (server dry run)" in dry_run_stdout:
            result = KubeCtl.dry_run_json_output(command, "name")
            if result.result[0] == "namespace":
                raise RuntimeError("Deleting a namespace is not allowed.")

            rollback_commands = [
                self._store_resource_state(
                    state_file,
                    result.result[0],
                    result.result[1],
                    namespace,
                )
            ]
        elif "autoscaled (server dry run)" in dry_run_stdout:
            hpa = KubeCtl.dry_run_json_output(command, "name")
            result = KubeCtl.dry_run_json_output(command, [".spec.scaleTargetRef.kind", ".metadata.name"])
            rollback_commands = [
                RollbackCommand(
                    "command",
                    "kubectl delete {resource_type} {resource_name} {nsp_flag_ctnt}".format(
                        resource_type=hpa.result[0],
                        resource_name=hpa.result[1],
                        nsp_flag_ctnt=nsp_flag_ctnt,
                    ),
                ),
                self._store_resource_state(
                    state_file,
                    result.result[0],
                    result.result[1],
                    namespace,
                ),
            ]
        else:
            result = KubeCtl.dry_run_json_output(command, "name")
            rollback_commands = [
                self._store_resource_state(
                    state_file,
                    result.result[0],
                    result.result[1],
                    namespace,
                )
            ]

        # Generate validation information
        if self.config.validate_rollback:
            time.sleep(self.config.retry_wait_time)
            full_state_file = os.path.join(state_dir, f"validation_{timestamp}_{cmd_hash}.yaml")
            full_state = RollbackTool.get_namespace_state(self.config.namespace)
            full_state = cleanup_kubernetes_yaml(full_state)
            with open(full_state_file, "w") as f:
                f.write(full_state)

        return_value = RollbackNode(action=command, rollback=rollback_commands, cluster_state=full_state_file)

        logger.info(f"Generated rollback action {rollback_commands} for '{command}'.")
        if self.config.validate_rollback:
            logger.info(f"Namespace state stored in: {full_state_file}")

        return return_value

    def _store_resource_state(
        self, state_file: str, resource_type: str, resource_name: str, namespace: str | None
    ) -> RollbackCommand:
        namespace_flag = f"-n {namespace}" if namespace else ""

        if resource_name is not None:
            state_cmd = f"kubectl get {resource_type} {resource_name} {namespace_flag} -o yaml"
        else:
            state_cmd = f"kubectl get {resource_type} {namespace_flag} -o yaml"

        logger.info(f"Capturing cluster state with: {state_cmd}")

        cluster_state = KubeCtl.exec_command_result(state_cmd)

        with open(state_file, "w") as f:
            cleaned_state = cleanup_kubernetes_yaml(cluster_state)
            f.write(cleaned_state)

        return RollbackCommand("file", state_file)
