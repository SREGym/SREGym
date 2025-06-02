"""Official example mcp client from anthropic, source: https://gist.github.com/zckly/f3f28ea731e096e53b39b47bf0a2d4b1"""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from init_backend import get_llm_backend_for_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()  # load environment variables from .env


class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.anthropic = Anthropic()

    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server

        Args:
            server_script_path: Path to the server script (.py or .js)
        """
        is_python = server_script_path.endswith(".py")
        is_js = server_script_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = (
            sys.executable  # Uses the current Python interpreter from the activated venv
            if is_python
            else "node"
        )
        server_params = StdioServerParameters(
            command=command, args=[server_script_path], env=None
        )

        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        self.stdio, self.write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(self.stdio, self.write)
        )

        await self.session.initialize()

        # List available tools
        response = await self.session.list_tools()
        tools = response.tools
        print("\nConnected to server with tools:", [tool.name for tool in tools])

    async def process_query(self, query: str) -> str:
        """Process a query using Claude and available tools"""
        messages = query

        response = await self.session.list_tools()
        # to make tool calling work on openai.
        available_tools = []
        tool_names = []
        for tool in response.tools:
            # FIXME: this is just to make this demo work, see below
            tool_names.append(tool.name)
            for param in tool.inputSchema["properties"].values():
                param["description"] = param["title"]
            print(f"tool input schema to openai: {tool.inputSchema}")
            # FIXME: When building MCP server tools, compile such object within the definition
            #  so that the client can use it directly.
            available_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema,
                    },
                }
            )

        llm = get_llm_backend_for_tools()
        finish_reason, response_message = llm.inference(
            system_prompt="You are a helpful assistant",
            input=messages,
            tools=available_tools,
        )

        # Process response and handle tool calls
        tool_results = []
        final_text = []

        if finish_reason == "tool_calls" or finish_reason in tool_names:
            tool_name = finish_reason
            tool_args = response_message
        else:
            tool_name = None
            tool_args = None

        print(f"tool {tool_name}, args {tool_args}")
        # Execute tool call
        if finish_reason == "stop":
            final_text.append(response_message.content)
        else:
            result = await self.session.call_tool(tool_name, tool_args)
            tool_results.append({"call": tool_name, "result": result})
            print(f"tool result: {result}")
            final_text.append(f"[Calling tool {tool_name} with args {tool_args}]")

        return "\n".join(final_text)

    async def chat_loop(self):
        """Run an interactive chat loop"""
        print("\nMCP Client Started!")
        print("Type your queries or 'quit' to exit.")

        while True:
            try:
                query = input("\nQuery: ").strip()

                if query.lower() == "quit":
                    break

                response = await self.process_query(query)
                print("\n" + response)

            except Exception as e:
                print(f"\nError: {str(e)}")

    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()


async def main():
    if len(sys.argv) < 2:
        print("Usage: python client.py <path_to_server_script>")
        sys.exit(1)

    client = MCPClient()
    try:
        await client.connect_to_server(sys.argv[1])
        await client.chat_loop()
    finally:
        await client.cleanup()


if __name__ == "__main__":
    import sys

    asyncio.run(main())
