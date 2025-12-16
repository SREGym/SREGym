# LLM-Based Meta-Agent Optimizer

## Quick Start

The LLM-enhanced meta-agent uses Gemini Pro Flash (or other LLMs) to optimize Stratus agent prompts and configurations based on execution traces and reward specifications.

### Setup

1. **Set API Key:**
```bash
export GOOGLE_API_KEY='your-api-key-here'
# or
export GEMINI_API_KEY='your-api-key-here'
```

2. **Run Example:**
```bash
python meta_agent_llm_example.py
```

### Basic Usage

```python
from meta_agent.llm_meta_agent import LLMMetaAgent, LLMMetaAgentConfig
from meta_agent.llm_optimizer import RewardSpec

# Define optimization objectives
reward_spec = RewardSpec(
    success_weight=1.0,      # Maximize success rate
    latency_weight=-0.5,     # Minimize latency
    attempts_weight=-0.3     # Minimize number of attempts
)

# Initialize meta-agent
config = LLMMetaAgentConfig(
    llm_model="gemini-pro-flash",
    use_llm_optimization=True,
    reward_spec=reward_spec
)

meta_agent = LLMMetaAgent(config)

# Run optimization cycle
result = meta_agent.start_learning_cycle()
```

## What It Does

### 1. **Reward Specification**

You specify what to optimize for:
- **Success rate**: How often agents succeed
- **Latency**: How fast agents complete tasks
- **Number of attempts**: How many tool calls agents make

Example configurations:
- **Maximize success**: `RewardSpec(success_weight=1.0, latency_weight=-0.1, attempts_weight=-0.1)`
- **Balance all**: `RewardSpec(success_weight=1.0, latency_weight=-0.5, attempts_weight=-0.3)`
- **Focus on efficiency**: `RewardSpec(success_weight=0.8, latency_weight=-1.0, attempts_weight=-0.8)`

### 2. **Trace Analysis**

The system analyzes execution traces to extract:
- Performance metrics (success rate, latency, attempts)
- Tool usage patterns (what tools work well)
- Reasoning patterns (what thinking strategies succeed)
- Success vs failure patterns

### 3. **LLM-Based Optimization**

Uses an LLM (Gemini Pro Flash) to:
- Analyze current prompts/configs
- Understand performance patterns
- Generate optimized prompts that:
  - Improve success rate (learn from successful patterns)
  - Reduce latency (make instructions more efficient)
  - Reduce attempts (guide agents to use fewer, more effective tool calls)

### 4. **Config Optimization**

Also optimizes agent configurations:
- `max_step`: How many tool calls agents can make
- `max_retry_attempts`: Retry behavior
- Other config parameters

## Architecture

```
Traces → Trace Analysis → Metrics + Patterns
                              ↓
                      Reward Specification
                              ↓
                    LLM (Gemini Pro Flash)
                              ↓
                  Optimized Prompts/Configs
                              ↓
                    Version Control & Save
```

## Key Features

1. **Flexible Reward Specification**: Customize optimization objectives
2. **Dual Optimization**: Combines pattern-based and LLM-based optimization
3. **Agent-Specific**: Optimizes each agent (diagnosis, localization, mitigation, rollback) separately
4. **Holistic Improvements**: LLM understands semantics and can make sophisticated improvements
5. **Error Handling**: Gracefully falls back to pattern-based optimization if LLM fails
6. **Version Control**: Maintains history of optimizations for rollback

## Files

- `meta_agent/llm_optimizer.py`: Core LLM optimization logic
- `meta_agent/llm_meta_agent.py`: LLM-enhanced meta-agent integration
- `meta_agent_llm_example.py`: Example usage script
- `meta_agent/LLM_OPTIMIZER_DESIGN.md`: Detailed design documentation

## Design Decisions Explained

### Why LLM-Based Optimization?

1. **Semantic Understanding**: LLMs understand context and can make holistic improvements that rule-based systems miss
2. **Natural Language**: Prompts are natural language, so LLMs excel at improving them
3. **Pattern Recognition**: LLMs can identify subtle patterns in successful vs failed executions
4. **Creative Solutions**: Can suggest improvements humans might not think of

### Why Reward Specification?

1. **Flexibility**: Different use cases have different priorities
2. **Explicit Objectives**: Makes optimization goals clear and debuggable
3. **Balanced Optimization**: Prevents over-optimizing one metric at expense of others
4. **Domain Expert Input**: Allows SREs to express their preferences

### Why Separate Prompt and Config Optimization?

1. **Different Concerns**: Prompts affect behavior, configs affect constraints
2. **Different Optimization Strategies**: Different analysis approaches needed
3. **Independent Evolution**: Can optimize one without affecting the other
4. **Specialized LLM Prompts**: Each gets a tailored optimization prompt

## Limitations

1. **LLM Dependency**: Requires working LLM API access
2. **API Costs**: Each optimization cycle has API costs
3. **Latency**: Slower than pattern-based optimization
4. **Minimum Traces**: Need sufficient traces (5-10 per agent) for meaningful optimization
5. **Validation Needed**: Should test optimized prompts on validation set

## Best Practices

1. **Collect Traces**: Run agents with trace collection enabled to gather data
2. **Set Appropriate Weights**: Balance reward specification based on operational needs
3. **Validate Changes**: Test optimized prompts on held-out problems
4. **Monitor Performance**: Track metrics after optimization to ensure improvements
5. **Iterate**: Run multiple optimization cycles over time as more data becomes available
6. **Version Control**: Use versioned prompts to enable rollback if needed

## Example Workflow

1. **Collect Traces**: Run Stratus agents on problems, traces are collected automatically
2. **Define Objectives**: Set reward specification based on priorities
3. **Run Optimization**: Execute LLM optimization cycle
4. **Review Changes**: Check optimized prompts/configs in config files
5. **Test**: Run optimized agents on validation problems
6. **Deploy**: If improvements confirmed, use optimized prompts
7. **Iterate**: Repeat as more traces are collected

## Output

Optimized prompts and configs are saved to:
- `clients/stratus/configs/*_agent_prompts.yaml`
- `clients/stratus/configs/*_agent_config.yaml`

Versioned copies are saved to:
- `meta_agent/versions/*_v*.yaml`

## Troubleshooting

**Issue**: "LLM backend not initialized"
- **Solution**: Check that `GOOGLE_API_KEY` or `GEMINI_API_KEY` is set

**Issue**: "Not enough traces for optimization"
- **Solution**: Collect more traces by running agents with trace collection enabled

**Issue**: "LLM API rate limit"
- **Solution**: The system handles this gracefully, will fall back to pattern-based optimization

**Issue**: "Optimized prompt seems worse"
- **Solution**: Use version control to rollback, or adjust reward specification

## Next Steps

See `LLM_OPTIMIZER_DESIGN.md` for detailed design documentation and implementation details.

