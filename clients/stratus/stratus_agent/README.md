# Agent Design and Implementation
Though fancy on text, all four stratus agents share mostly the same agent implementation. They are implemented as custom REACT agents, which all inherit from the `BaseAgent` class in [`base_agent.py`](https://github.com/xlab-uiuc/SREArena/blob/main/clients/stratus/stratus_agent/base_agent.py).
Different agents, such as `DiagnosisAgent` and `MitigationAgent`, are distinguished by their prompts and tool usage.

In every round, the agent gets a "thinking step," where it chooses a tool and justify its usage. It then gets an "action step," where it generates the correct tool call for the chosen tool.

After a step limit, the agent is forced to submit a result to the benchmark.
