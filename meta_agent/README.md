# Meta-Agent System for Iterative Agent Improvement

A comprehensive system that learns from agent execution traces to iteratively improve agent guidelines and prompts in the Stratus system.

## Overview

The Meta-Agent system implements a continuous learning loop that:

1. **Collects Traces**: Records agent execution data including tool usage, thinking steps, and outcomes
2. **Analyzes Patterns**: Identifies successful strategies, failure modes, and performance patterns
3. **Generates Guidelines**: Updates agent prompts based on learned patterns
4. **Manages Versions**: Maintains version control for all prompt changes
5. **Iterates Continuously**: Runs learning cycles to continuously improve agent performance

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│   Trace         │    │   Pattern        │    │   Guideline         │
│   Collector     │───▶│   Analyzer       │───▶│   Generator         │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│   Agent         │    │   Pattern        │    │   Versioned         │
│   Traces        │    │   Insights       │    │   Prompts           │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
```

## Components

### 1. Trace Collector (`trace_collector.py`)

Records and manages agent execution traces:

- **Tool Calls**: Records each tool usage with parameters, success status, and duration
- **Thinking Steps**: Captures reasoning and decision-making process
- **Problem Context**: Stores information about the problem being solved
- **Performance Metrics**: Tracks execution time, success rates, and other metrics

### 2. Pattern Analyzer (`pattern_analyzer.py`)

Analyzes traces to identify patterns:

- **Success Patterns**: Identifies effective tool sequences and strategies
- **Failure Patterns**: Finds common failure points and problematic tools
- **Tool Effectiveness**: Measures tool success rates and performance
- **Thinking Patterns**: Analyzes reasoning quality and decision-making
- **Performance Patterns**: Identifies optimization opportunities

### 3. Guideline Generator (`guideline_generator.py`)

Updates agent guidelines based on learned patterns:

- **Prompt Updates**: Modifies agent prompts with learned insights
- **Version Control**: Maintains history of all prompt changes
- **Rollback Support**: Allows reverting to previous prompt versions
- **Change Tracking**: Records what patterns were applied and when

### 4. Meta-Agent Orchestrator (`meta_agent.py`)

Coordinates the learning process:

- **Learning Cycles**: Manages iterative learning process
- **Configuration**: Handles system configuration and parameters
- **State Management**: Tracks learning progress and performance
- **Continuous Learning**: Runs automated learning cycles

### 5. Integration Layer (`integration.py`)

Provides easy integration with existing Stratus agents:

- **Decorators**: Simple decorators for automatic trace collection
- **Hooks**: Integration points for existing agent code
- **Manager**: High-level interface for managing the meta-agent system

## Usage

### Basic Setup

```python
from meta_agent import MetaAgent, MetaAgentConfig
from meta_agent.trace_collector import AgentType, ProblemContext

# Initialize meta-agent
config = MetaAgentConfig(
    learning_interval=3600,  # 1 hour between learning cycles
    min_traces_for_analysis=10,
    confidence_threshold=0.7,
    enable_auto_updates=True
)

meta_agent = MetaAgent(config)
```

### Manual Trace Collection

```python
# Start a trace
problem_context = ProblemContext(
    problem_id="problem_123",
    app_name="hotel-reservation",
    app_namespace="hotel-reservation",
    app_description="A microservices-based hotel reservation system"
)

trace = meta_agent.collect_agent_trace(
    trace_id="trace_001",
    agent_type=AgentType.DIAGNOSIS,
    problem_context=problem_context
)

# Record tool calls
meta_agent.add_tool_call(
    trace_id="trace_001",
    tool_name="get_pods",
    arguments={"namespace": "hotel-reservation"},
    success=True,
    response="Found 5 pods running",
    duration=1.2
)

# Record thinking steps
meta_agent.add_thinking_step(
    trace_id="trace_001",
    reasoning="I need to check pod status first",
    tool_choice="get_pods",
    justification="This will give me visibility into the system"
)

# End trace
meta_agent.end_agent_trace(
    trace_id="trace_001",
    success=True,
    final_submission="Database connection issue identified"
)
```

### Using Integration Decorators

```python
from meta_agent.integration import trace_agent_execution, trace_tool_call

@trace_agent_execution(AgentType.DIAGNOSIS)
def diagnose_problem(problem_id: str) -> Dict[str, Any]:
    """Your existing diagnosis function"""
    
    @trace_tool_call("current_trace_id")
    def get_pod_status(namespace: str) -> str:
        # Your tool implementation
        return "Pods are running"
    
    # Your diagnosis logic here
    return {"status": "healthy"}
```

### Running Learning Cycles

```python
# Start a learning cycle
result = meta_agent.start_learning_cycle()
print(f"Learning result: {result}")

# Get learning status
status = meta_agent.get_learning_status()
print(f"Status: {status}")

# Get learned patterns
patterns = meta_agent.get_pattern_summary()
print(f"Patterns: {patterns}")
```

### Continuous Learning

```python
# Run continuous learning (blocks until interrupted)
meta_agent.run_continuous_learning()
```

## Configuration

### MetaAgentConfig Parameters

- `learning_interval`: Seconds between learning cycles (default: 3600)
- `min_traces_for_analysis`: Minimum traces needed for analysis (default: 10)
- `confidence_threshold`: Minimum confidence for applying patterns (default: 0.7)
- `enable_auto_updates`: Automatically apply guideline updates (default: True)
- `backup_original_prompts`: Backup original prompts before updates (default: True)

### Environment Variables

- `META_AGENT_CONFIG_FILE`: Path to configuration file
- `META_AGENT_STATE_FILE`: Path to state persistence file
- `META_AGENT_LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)

## Data Storage

### Trace Storage

Traces are stored in JSON format in the `meta_agent/traces/` directory:

```
meta_agent/traces/
├── diagnosis_problem_123_20241201_120000_trace_001.json
├── localization_problem_456_20241201_130000_trace_002.json
└── mitigation_problem_789_20241201_140000_trace_003.json
```

### Version Control

Prompt versions are stored in the `meta_agent/versions/` directory:

```
meta_agent/versions/
├── diagnosis_v1.0.0.yaml
├── diagnosis_v1.0.1.yaml
├── localization_v1.0.0.yaml
└── mitigation_v1.0.0.yaml
```

### State Persistence

Meta-agent state is saved to `meta_agent/state.json`:

```json
{
  "config": {
    "learning_interval": 3600,
    "min_traces_for_analysis": 10,
    "confidence_threshold": 0.7
  },
  "learning_state": {
    "last_learning_time": 1701432000.0,
    "learning_cycles": 5,
    "total_patterns_learned": 23
  },
  "performance_history": [...]
}
```

## Pattern Types

### Success Patterns

Identifies effective tool sequences and strategies:

```python
{
  "pattern_type": "success_pattern",
  "description": "Successful tool sequence: get_pods -> get_logs -> check_services",
  "confidence": 0.85,
  "frequency": 12,
  "recommendations": [
    "Consider using the sequence get_pods -> get_logs -> check_services for similar problems",
    "This pattern has shown high success rate in past executions"
  ]
}
```

### Failure Patterns

Identifies common failure points:

```python
{
  "pattern_type": "failure_pattern", 
  "description": "Common failure point: get_metrics",
  "confidence": 0.9,
  "frequency": 8,
  "recommendations": [
    "Review usage of get_metrics tool",
    "Consider adding error handling or validation before calling this tool"
  ]
}
```

### Tool Effectiveness

Measures tool performance:

```python
{
  "pattern_type": "tool_effectiveness",
  "description": "Highly effective tool: get_pods",
  "confidence": 0.92,
  "frequency": 25,
  "recommendations": [
    "Prioritize using get_pods when appropriate",
    "This tool has 92% success rate"
  ]
}
```

## Integration with Stratus Agents

### Existing Agent Modification

To integrate with existing Stratus agents, add decorators to key functions:

```python
# In diagnosis_agent.py
from meta_agent.integration import trace_agent_execution, trace_tool_call

class DiagnosisAgent(BaseAgent):
    @trace_agent_execution(AgentType.DIAGNOSIS)
    def run(self, state: State) -> State:
        # Existing agent logic
        pass
    
    @trace_tool_call("current_trace_id")
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        # Existing tool calling logic
        pass
```

### New Agent Development

For new agents, use the integration decorators from the start:

```python
from meta_agent.integration import trace_agent_execution

@trace_agent_execution(AgentType.MITIGATION)
def new_mitigation_agent(problem_context: ProblemContext) -> Dict[str, Any]:
    # Agent implementation
    pass
```

## Monitoring and Debugging

### Learning Status

```python
status = meta_agent.get_learning_status()
print(f"Learning cycles: {status['learning_cycles']}")
print(f"Total patterns learned: {status['total_patterns_learned']}")
print(f"Ready for learning: {status['ready_for_learning']}")
```

### Pattern Analysis

```python
patterns = meta_agent.get_pattern_summary()
print(f"Total patterns: {patterns['total_patterns']}")
print(f"High confidence patterns: {patterns['high_confidence_patterns']}")
```

### Guideline History

```python
history = meta_agent.get_guideline_history()
for update in history:
    print(f"Agent: {update['agent_type']}, Version: {update['version']}")
    print(f"Patterns applied: {update['patterns_applied']}")
```

## Best Practices

### 1. Trace Quality

- Ensure traces capture meaningful decision points
- Include both successful and failed executions
- Record detailed reasoning in thinking steps

### 2. Pattern Confidence

- Use high confidence thresholds (0.7+) for production
- Validate patterns with additional data before applying
- Monitor pattern effectiveness after application

### 3. Version Management

- Regularly backup prompt versions
- Test new guidelines before deploying
- Keep rollback capabilities available

### 4. Performance Monitoring

- Monitor learning cycle performance
- Track agent improvement over time
- Set up alerts for learning failures

## Troubleshooting

### Common Issues

1. **Insufficient Traces**: Increase `min_traces_for_analysis` or collect more traces
2. **Low Pattern Confidence**: Lower `confidence_threshold` or improve trace quality
3. **Learning Failures**: Check logs for specific error messages
4. **Prompt Corruption**: Use rollback functionality to restore previous versions

### Debug Mode

Enable debug logging:

```python
import logging
logging.getLogger("meta_agent").setLevel(logging.DEBUG)
```

### State Recovery

Load previous state:

```python
meta_agent.load_state("meta_agent/state.json")
```

## Future Enhancements

- **Multi-Agent Learning**: Cross-agent pattern sharing
- **Real-time Learning**: Immediate pattern application
- **Advanced Analytics**: More sophisticated pattern analysis
- **A/B Testing**: Compare different prompt versions
- **Performance Prediction**: Predict agent performance before execution



