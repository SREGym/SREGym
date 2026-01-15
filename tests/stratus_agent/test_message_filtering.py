"""
Unit tests for rejected command error message filtering in BaseAgent.

Tests the functionality that removes "Command Rejected" error messages from
Stratus' context window after a successful command execution.
"""

import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from clients.stratus.stratus_agent.base_agent import BaseAgent


class TestMessageFiltering(unittest.TestCase):
    """Test cases for the _filter_rejected_command_errors method."""

    def setUp(self):
        """Set up a mock BaseAgent for testing."""
        # Mock the dependencies
        with patch("clients.stratus.stratus_agent.base_agent.StateGraph"):
            self.agent = BaseAgent(
                llm=MagicMock(),
                max_step=10,
                sync_tools=[],
                async_tools=[],
                submit_tool=MagicMock(),
                tool_descs="test tools",
            )
            # Mock the logger to avoid log output during tests
            self.agent.local_logger = MagicMock()

    def test_no_filtering_when_no_messages(self):
        """Test that empty message list returns empty list."""
        messages = []
        result = self.agent._filter_rejected_command_errors(messages)
        self.assertEqual(result, [])

    def test_no_filtering_when_single_message(self):
        """Test that single message is not filtered."""
        messages = [HumanMessage(content="test")]
        result = self.agent._filter_rejected_command_errors(messages)
        self.assertEqual(result, messages)

    def test_no_filtering_when_last_message_not_tool_message(self):
        """Test no filtering when last message is not a ToolMessage."""
        messages = [
            HumanMessage(content="test"),
            AIMessage(content="response"),
        ]
        result = self.agent._filter_rejected_command_errors(messages)
        self.assertEqual(result, messages)

    def test_no_filtering_when_last_message_is_rejection(self):
        """Test that rejection messages are kept when the last message is also a rejection."""
        messages = [
            HumanMessage(content="execute command"),
            AIMessage(content="calling tool", tool_calls=[{"id": "1", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="1"),
        ]
        result = self.agent._filter_rejected_command_errors(messages)
        self.assertEqual(len(result), 3)
        self.assertEqual(result, messages)

    def test_filters_rejected_command_after_success(self):
        """Test that rejected commands are filtered after a successful execution."""
        messages = [
            HumanMessage(content="execute command"),
            # First attempt - rejected
            AIMessage(content="trying pipe", tool_calls=[{"id": "1", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="1"),
            # Second attempt - successful
            AIMessage(content="trying without pipe", tool_calls=[{"id": "2", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="pod/nginx-123 running", tool_call_id="2"),
        ]

        result = self.agent._filter_rejected_command_errors(messages)

        # Should have removed the rejected AIMessage and ToolMessage
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], messages[0])  # HumanMessage
        self.assertEqual(result[1], messages[3])  # Successful AIMessage
        self.assertEqual(result[2], messages[4])  # Successful ToolMessage

    def test_filters_multiple_rejected_commands(self):
        """Test that multiple rejected commands are all filtered after success."""
        messages = [
            HumanMessage(content="execute command"),
            # First attempt - rejected
            AIMessage(content="trying pipe", tool_calls=[{"id": "1", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="1"),
            # Second attempt - rejected
            AIMessage(content="trying redirect", tool_calls=[{"id": "2", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(
                content="Command Rejected (ValueError): Write redirection is forbidden.", tool_call_id="2"
            ),
            # Third attempt - successful
            AIMessage(content="trying plain command", tool_calls=[{"id": "3", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="pod/nginx-123 running", tool_call_id="3"),
        ]

        result = self.agent._filter_rejected_command_errors(messages)

        # Should have removed both rejected attempts (4 messages total: 2 AI + 2 Tool)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], messages[0])  # HumanMessage
        self.assertEqual(result[1], messages[5])  # Successful AIMessage
        self.assertEqual(result[2], messages[6])  # Successful ToolMessage

    def test_preserves_non_kubectl_tool_messages(self):
        """Test that non-kubectl tool messages are preserved."""
        messages = [
            HumanMessage(content="execute command"),
            # Other tool call
            AIMessage(content="calling other tool", tool_calls=[{"id": "1", "name": "submit_tool"}]),
            ToolMessage(content="Submitted successfully", tool_call_id="1"),
            # Rejected kubectl command
            AIMessage(content="trying pipe", tool_calls=[{"id": "2", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="2"),
            # Successful kubectl command
            AIMessage(content="trying plain", tool_calls=[{"id": "3", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="pod/nginx-123 running", tool_call_id="3"),
        ]

        result = self.agent._filter_rejected_command_errors(messages)

        # Should preserve the submit_tool messages and remove only the rejected kubectl message
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0], messages[0])  # HumanMessage
        self.assertEqual(result[1], messages[1])  # submit_tool AIMessage
        self.assertEqual(result[2], messages[2])  # submit_tool ToolMessage
        self.assertEqual(result[3], messages[5])  # Successful kubectl AIMessage
        self.assertEqual(result[4], messages[6])  # Successful kubectl ToolMessage

    def test_preserves_system_and_human_messages(self):
        """Test that system and human messages are always preserved."""
        messages = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="user request"),
            # Rejected command
            AIMessage(content="trying pipe", tool_calls=[{"id": "1", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="1"),
            HumanMessage(content="try again"),
            # Successful command
            AIMessage(content="trying plain", tool_calls=[{"id": "2", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="pod/nginx-123 running", tool_call_id="2"),
        ]

        result = self.agent._filter_rejected_command_errors(messages)

        # Should preserve all System and Human messages
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0], messages[0])  # SystemMessage
        self.assertEqual(result[1], messages[1])  # HumanMessage
        self.assertEqual(result[2], messages[4])  # HumanMessage "try again"
        self.assertEqual(result[3], messages[5])  # Successful AIMessage
        self.assertEqual(result[4], messages[6])  # Successful ToolMessage

    def test_logs_filtering_info(self):
        """Test that filtering logs appropriate messages."""
        messages = [
            HumanMessage(content="execute command"),
            AIMessage(content="trying pipe", tool_calls=[{"id": "1", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="1"),
            AIMessage(content="trying plain", tool_calls=[{"id": "2", "name": "exec_kubectl_cmd_safely"}]),
            ToolMessage(content="pod/nginx-123 running", tool_call_id="2"),
        ]

        self.agent._filter_rejected_command_errors(messages)

        # Verify that logging was called
        self.agent.local_logger.info.assert_called()
        # Check that the log message contains information about filtering
        log_call_args = str(self.agent.local_logger.info.call_args)
        self.assertIn("Filtered", log_call_args)
        self.assertIn("rejected command", log_call_args.lower())


class TestPostRoundProcessIntegration(unittest.TestCase):
    """Integration tests for post_round_process with message filtering."""

    def setUp(self):
        """Set up a mock BaseAgent for testing."""
        with patch("clients.stratus.stratus_agent.base_agent.StateGraph"):
            self.agent = BaseAgent(
                llm=MagicMock(),
                max_step=10,
                sync_tools=[],
                async_tools=[],
                submit_tool=MagicMock(),
                tool_descs="test tools",
            )
            self.agent.local_logger = MagicMock()
            self.agent.arena_logger = MagicMock()
            self.agent.loop_count = 0

    def test_post_round_process_returns_filtered_messages(self):
        """Test that post_round_process returns filtered messages."""
        state = {
            "num_steps": 0,
            "submitted": False,
            "messages": [
                HumanMessage(content="execute command"),
                AIMessage(content="trying pipe", tool_calls=[{"id": "1", "name": "exec_kubectl_cmd_safely"}]),
                ToolMessage(content="Command Rejected: Pipe commands are forbidden", tool_call_id="1"),
                AIMessage(content="trying plain", tool_calls=[{"id": "2", "name": "exec_kubectl_cmd_safely"}]),
                ToolMessage(content="pod/nginx-123 running", tool_call_id="2"),
            ],
            "rollback_stack": "",
        }

        result = self.agent.post_round_process(state)

        # Should increment num_steps
        self.assertEqual(result["num_steps"], 1)

        # Should filter messages
        self.assertIn("messages", result)
        self.assertEqual(len(result["messages"]), 3)
        # First message should be the HumanMessage
        self.assertIsInstance(result["messages"][0], HumanMessage)
        # Last message should be the successful ToolMessage
        self.assertIsInstance(result["messages"][-1], ToolMessage)
        self.assertIn("running", result["messages"][-1].content)


if __name__ == "__main__":
    unittest.main()
