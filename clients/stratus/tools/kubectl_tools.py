import logging
from contextlib import AsyncExitStack
from typing import Annotated, Any, Optional

from fastmcp import Client
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId
from langchain_core.tools.base import ArgsSchema, BaseTool
from langgraph.types import Command
from pydantic import BaseModel, Field, PrivateAttr

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("all.stratus.tools")


class ExecKubectlCmdSafelyInput(BaseModel):
    command: str = Field(
        description="The command you want to execute in a CLI to manage a k8s cluster. "
        "It should start with 'kubectl'. Converts natural language to kubectl commands and executes them. "
        "Can be used to get/describe/edit Kubernetes deployments, services, and other Kubernetes components. "
        "Only takes one query at a time. Keep queries simple and straight-forward. "
        "This tool cannot handle complex mutli-step queries. "
        "Remember that most kubectl queries require a namespace name. "
    )
    tool_call_id: Annotated[str, InjectedToolCallId]


class ExecKubectlCmdSafely(BaseTool):
    name: str = "exec_kubectl_cmd_safely"
    description: str = "this is a tool used to safely execute kubectl commands."
    args_schema: Optional[ArgsSchema] = ExecKubectlCmdSafelyInput

    _client: Client = PrivateAttr()

    def __init__(self, client: Client, **kwargs: Any):
        super().__init__(**kwargs)
        self._client = client

    def _run(self):
        assert False, f"{self.name} is an async method, you are running it as a sync method!"
        pass

    async def _arun(
        self,
        command: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        logger.debug(f"tool_call_id in {self.name}: {tool_call_id}")
        logger.debug(
            f"calling mcp exec_kubectl_cmd_safely from " f'langchain exec_kubectl_cmd_safely, with command: "{command}"'
        )
        exit_stack = AsyncExitStack()
        try:
            await exit_stack.enter_async_context(self._client)
            result = await self._client.call_tool("exec_kubectl_cmd_safely", arguments={"cmd": command})
            # Handle CallToolResult - it has a .content attribute with a list of parts
            if hasattr(result, 'content') and result.content:
                text_result = "\n".join([part.text for part in result.content])
            elif hasattr(result, 'text'):
                text_result = result.text
            else:
                # Fallback: try to convert to string
                text_result = str(result)
        finally:
            await exit_stack.aclose()
        return Command(
            update={
                "messages": [
                    ToolMessage(content=text_result, tool_call_id=tool_call_id),
                ]
            }
        )


kubectl_read_only_cmds = [
    "kubectl api-resources",
    "kubectl api-version",
    # read only if not interactive (interactive commands are prohibited)
    "kubectl attach",
    "kubectl auth can-i",
    "kubectl cluster-info",
    "kubectl describe",
    "kubectl diff",
    "kubectl events",
    "kubectl explain",
    "kubectl get",
    "kubectl logs",
    "kubectl options",
    "kubectl top",
    "kubectl version",
    "kubectl config view",
    "kubectl config current-context",
    "kubectl config get",
]


class ExecReadOnlyKubectlCmdInput(BaseModel):
    command: str = Field(
        description=f"The read-only kubectl command you want to execute in a CLI "
        'to manage a k8s cluster. It should start with "kubectl". '
        f"Available Read-only Commands: {kubectl_read_only_cmds}"
    )
    tool_call_id: Annotated[str, InjectedToolCallId]


class ExecReadOnlyKubectlCmd(BaseTool):
    name: str = "exec_read_only_kubectl_cmd"
    description: str = "this is a tool used to execute read-only kubectl commands."
    args_schema: Optional[ArgsSchema] = ExecReadOnlyKubectlCmdInput

    _client: Client = PrivateAttr()

    def __init__(self, client: Client, **kwargs: Any):
        super().__init__(**kwargs)
        self._client = client

    def _run(self):
        assert False, f"{self.name} is an async method, you are running it as a sync method!"
        pass

    async def _arun(
        self,
        command: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        logger.debug(f"tool_call_id in {self.name}: {tool_call_id}")
        is_read_only = False
        for c in kubectl_read_only_cmds:
            if command.startswith(c):
                is_read_only = True
                break
        if not is_read_only:
            logger.debug(
                f"Agent is trying to exec a non read-only command {command} " f"with tool exec_read_only_kubectl_cmd"
            )
            text_result = (
                f"Your command {command} is not a read-only kubectl command. "
                f"Available Read-only Commands: {kubectl_read_only_cmds}."
            )
        elif command.startswith("kubectl logs -f"):
            logger.debug(f"agent calling interactive read-only command")
            text_result = (
                f"Your command {command} is an _interactive_ read-only kubectl command. " f"It is not supported!"
            )
        else:
            logger.debug(
                f"calling mcp exec_kubectl_cmd_safely from "
                f'langchain exec_read_only_kubectl_cmd, with command: "{command}"'
            )
            exit_stack = AsyncExitStack()
            try:
                await exit_stack.enter_async_context(self._client)
                result = await self._client.call_tool("exec_kubectl_cmd_safely", arguments={"cmd": command})
                # Handle CallToolResult - it has a .content attribute with a list of parts
                if hasattr(result, 'content') and result.content:
                    text_result = "\n".join([part.text for part in result.content])
                elif hasattr(result, 'text'):
                    text_result = result.text
                else:
                    # Fallback: try to convert to string
                    text_result = str(result)
            finally:
                await exit_stack.aclose()
        return Command(
            update={
                "messages": [
                    ToolMessage(content=text_result, tool_call_id=tool_call_id),
                ]
            }
        )


class RollbackCommandCmdInput(BaseModel):
    tool_call_id: Annotated[str, InjectedToolCallId]


class RollbackCommand(BaseTool):
    name: str = "rollback_command"
    description: str = (
        "Use this function to roll back the last kubectl command "
        'you successfully executed with the "exec_kubectl_cmd_safely" tool.'
    )
    args_schema: Optional[ArgsSchema] = RollbackCommandCmdInput

    _client: Client = PrivateAttr()

    def __init__(self, client: Client, **kwargs: Any):
        super().__init__(**kwargs)
        self._client = client

    def _run(self):
        assert False, f"{self.name} is an async method, you are running it as a sync method!"
        pass

    async def _arun(
        self,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        logger.debug(f"tool_call_id in {self.name}: {tool_call_id}")
        logger.debug(f"calling langchain rollback_command")
        exit_stack = AsyncExitStack()
        try:
            await exit_stack.enter_async_context(self._client)
            result = await self._client.call_tool("rollback_command")
            # Handle CallToolResult - it has a .content attribute with a list of parts
            if hasattr(result, 'content') and result.content:
                text_result = "\n".join([part.text for part in result.content])
            elif hasattr(result, 'text'):
                text_result = result.text
            else:
                # Fallback: try to convert to string
                text_result = str(result)
        finally:
            await exit_stack.aclose()
        return Command(
            update={
                "rollback_stack": str(text_result),
                "messages": [
                    ToolMessage(content=text_result, tool_call_id=tool_call_id),
                ],
            }
        )


class GetPreviousRollbackableCmdInput(BaseModel):
    tool_call_id: Annotated[str, InjectedToolCallId]


class GetPreviousRollbackableCmd(BaseTool):
    name: str = "get_previous_rollbackable_cmd"
    description: str = (
        "Use this function to get a list of commands you "
        "previously executed that could be roll-backed. "
        'When you call "rollback_command" tool multiple times, '
        "you will roll-back previous commands in the order "
        "of the returned list."
    )
    args_schema: Optional[ArgsSchema] = GetPreviousRollbackableCmdInput

    _client: Client = PrivateAttr()

    def __init__(self, client: Client, **kwargs: Any):
        super().__init__(**kwargs)
        self._client = client

    def _run(self):
        assert False, f"{self.name} is an async method, you are running it as a sync method!"
        pass

    async def _arun(
        self,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        logger.debug(f"tool_call_id in {self.name}: {tool_call_id}")
        logger.debug(f"calling langchain get_previous_rollbackable_cmd")
        exit_stack = AsyncExitStack()
        try:
            await exit_stack.enter_async_context(self._client)
            result = await self._client.call_tool("get_previous_rollbackable_cmd")
            # Handle CallToolResult - it has a .content attribute with a list of parts
            if hasattr(result, 'content'):
                if len(result.content) == 0:
                    text_result = "There is no previous rollbackable command."
                else:
                    text_result = "\n".join([part.text for part in result.content])
            elif hasattr(result, '__len__') and len(result) == 0:
                text_result = "There is no previous rollbackable command."
            elif hasattr(result, 'text'):
                text_result = result.text
            else:
                # Fallback: try to convert to string
                text_result = str(result)
        finally:
            await exit_stack.aclose()
        return Command(
            update={
                "messages": [
                    ToolMessage(content=text_result, tool_call_id=tool_call_id),
                ]
            }
        )
