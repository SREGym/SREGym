from mcp_server.configs.kubectl_tool_cfg import KubectlToolCfg, output_parent_dir
from mcp_server.kubectl_server_helper.action_stack import ActionStack
from mcp_server.kubectl_server_helper.kubectl_cmd_runner import KubectlCmdRunner
from mcp_server.kubectl_server_helper.rollback_tool import RollbackTool


class KubectlToolSet:
    def __init__(self, session_id: str):
        self.ssid = session_id

        self.config = KubectlToolCfg(output_dir=str(output_parent_dir / self.ssid))

        self.action_stack = None
        if self.config.use_rollback_stack:
            self.action_stack = ActionStack()

        self.cmd_runner = KubectlCmdRunner(self.config, self.action_stack)
        self.rollback_tool = RollbackTool(self.config, self.action_stack)
