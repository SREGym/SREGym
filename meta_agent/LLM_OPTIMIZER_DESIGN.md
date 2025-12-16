# LLM-Based Meta-Agent Optimizer Design

## Overview

This document explains the design and implementation of the LLM-enhanced meta-agent system that uses Gemini Pro Flash (or other LLMs) to optimize Stratus agent prompts and configurations based on execution traces and reward specifications.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    LLM-Enhanced Meta-Agent                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────┐         ┌──────────────────────┐         │
│  │  Reward Spec     │         │   Trace Collector    │         │
│  │  - Success       │         │   - Collects agent   │         │
│  │  - Latency       │         │     execution data   │         │
│  │  - Attempts      │         │   - Tool calls       │         │
│  └────────┬─────────┘         │   - Thinking steps   │         │
│           │                    │   - Performance      │         │
│           │                    └──────────┬───────────┘         │
│           │                               │                     │
│           ▼                               ▼                     │
│  ┌──────────────────────────────────────────────────────┐      │
│  │          LLM Prompt Optimizer                        │      │
│  │  - Analyzes traces                                   │      │
│  │  - Computes metrics (success, latency, attempts)     │      │
│  │  - Calls LLM (Gemini Pro Flash) with:               │      │
│  │    * Current prompt                                  │      │
│  │    * Performance metrics                             │      │
│  │    * Reward specification                            │      │
│  │    * Successful/failed patterns                     │      │
│  │  - Receives optimized prompt                        │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐      │
│  │          LLM Config Optimizer                        │      │
│  │  - Analyzes config usage (max_step, etc.)           │      │
│  │  - Identifies bottlenecks                           │      │
│  │  - Calls LLM to optimize config values              │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐      │
│  │          Guideline Generator                         │      │
│  │  - Applies optimized prompts                         │      │
│  │  - Version control                                   │      │
│  │  - Saves to config files                            │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. Reward Specification System

**Why:** Provides a flexible way to specify optimization objectives without hardcoding priorities.

**Design:**
- `RewardSpec` class with configurable weights:
  - `success_weight`: Maximize success rate (positive weight)
  - `latency_weight`: Minimize latency (negative weight, because lower is better)
  - `attempts_weight`: Minimize number of attempts (negative weight)

**Example Usage:**
```python
# Prioritize success rate
reward_spec = RewardSpec(success_weight=1.0, latency_weight=-0.2, attempts_weight=-0.1)

# Balance all objectives
reward_spec = RewardSpec(success_weight=1.0, latency_weight=-0.5, attempts_weight=-0.3)

# Focus on efficiency
reward_spec = RewardSpec(success_weight=0.8, latency_weight=-1.0, attempts_weight=-0.8)
```

**Benefits:**
- Allows domain experts to express preferences
- Can be tuned based on operational requirements
- Makes optimization objectives explicit and debuggable

### 2. LLM-Based Optimization

**Why:** Pattern-based optimization (the existing approach) can miss subtle improvements and doesn't understand semantic relationships between prompt changes and outcomes. LLMs excel at:
- Understanding context and semantics
- Generating natural language improvements
- Making holistic changes to prompts
- Learning from examples

**Design:**
- Separate optimizers for prompts and configs
- Structured prompts to LLM with:
  - Current prompt/config
  - Performance metrics
  - Reward specification
  - Successful and failed patterns
- LLM returns JSON with optimized prompt/config and explanations

**Prompt Engineering:**
- Clear task description
- Context about agent's role
- Concrete performance data
- Specific optimization goals
- Structured output format (JSON)

**Example LLM Prompt Structure:**
```
1. Current state (prompt/config)
2. Performance metrics (success rate, latency, attempts)
3. Reward specification (what to optimize for)
4. Patterns from traces (what worked, what didn't)
5. Clear instructions on what to optimize
6. Structured output format
```

### 3. Trace Analysis and Metric Computation

**Why:** Raw traces are too verbose for LLM consumption. We need to extract meaningful metrics and patterns.

**Design:**
- `_analyze_traces()` method extracts:
  - Success rate
  - Average latency
  - Average number of attempts
  - Overall reward score
  - Tool usage patterns
  - Reasoning patterns (from thinking steps)
  - Successful vs failed patterns

**Benefits:**
- Reduces token usage for LLM calls
- Focuses on relevant information
- Makes patterns explicit

### 4. Dual Optimization Approach

**Why:** Combine the strengths of both approaches:
- Pattern-based optimization: Fast, deterministic, good for clear patterns
- LLM optimization: Semantic understanding, holistic improvements, handles complex cases

**Design:**
- `LLMMetaAgent` extends `MetaAgent`
- Runs both pattern analysis and LLM optimization
- Applies updates from both sources
- LLM optimization only runs if enough traces available (configurable threshold)

**Workflow:**
1. Collect traces
2. Run pattern analyzer (fast, deterministic)
3. If sufficient traces: Run LLM optimizer (slower, more intelligent)
4. Apply both types of updates
5. Version control and save

### 5. Agent-Specific Optimization

**Why:** Different agents (diagnosis, localization, mitigation, rollback) have different optimization needs.

**Design:**
- Traces grouped by agent type
- Separate optimization for each agent
- Agent-specific context in LLM prompts
- Can set different reward specs per agent

**Benefits:**
- Tailored optimizations for each agent's role
- Independent versioning
- Can optimize agents at different rates

### 6. Config Optimization

**Why:** Prompts aren't the only thing that affects performance. Configuration values like `max_step` can be bottlenecks.

**Design:**
- `LLMConfigOptimizer` analyzes:
  - How often agents hit step limits
  - Average steps taken
  - Success rates with current limits
- LLM suggests optimal config values based on usage patterns

**Example:**
- If many failures are due to hitting `max_step`, LLM suggests increasing it
- If agents consistently finish early, suggests reducing it to save resources

### 7. Error Handling and Fallbacks

**Why:** LLM APIs can fail, rate limit, or return malformed responses.

**Design:**
- Graceful degradation: Falls back to pattern-based optimization if LLM fails
- JSON parsing with fallbacks: Tries multiple extraction strategies
- Validation: Checks optimized prompts/configs before applying
- Logging: Detailed logs for debugging

**Resilience:**
- If LLM unavailable: System continues with pattern-based optimization
- If LLM returns invalid JSON: Falls back to current prompt/config
- If API rate limited: Logs error, continues with other optimizations

## Usage Example

```python
from meta_agent.llm_meta_agent import LLMMetaAgent, LLMMetaAgentConfig
from meta_agent.llm_optimizer import RewardSpec

# Define what to optimize for
reward_spec = RewardSpec(
    success_weight=1.0,      # Maximize success rate
    latency_weight=-0.5,     # Minimize latency
    attempts_weight=-0.3     # Minimize attempts
)

# Initialize meta-agent
config = LLMMetaAgentConfig(
    llm_model="gemini-pro-flash",
    use_llm_optimization=True,
    optimize_prompts=True,
    optimize_configs=True,
    reward_spec=reward_spec
)

meta_agent = LLMMetaAgent(config)

# Run optimization cycle
result = meta_agent.start_learning_cycle()
```

## Integration with Existing System

The LLM optimizer integrates seamlessly with the existing meta-agent framework:

1. **Trace Collection**: Uses existing `TraceCollector`
2. **Pattern Analysis**: Runs alongside existing `PatternAnalyzer`
3. **Guideline Updates**: Uses existing `GuidelineGenerator` for version control
4. **File Management**: Saves to existing config directories

## Performance Considerations

1. **LLM API Costs**: Each optimization cycle makes LLM API calls. Use `min_traces_for_llm_optimization` to control frequency.

2. **Latency**: LLM optimization is slower than pattern-based. Consider running asynchronously for production.

3. **Rate Limiting**: Gemini API has rate limits. The system handles errors gracefully.

4. **Token Usage**: Prompts are designed to be concise but informative. Trace analysis reduces token usage.

## Future Enhancements

1. **Multi-objective Optimization**: More sophisticated reward functions
2. **A/B Testing**: Test optimized prompts on subset of problems
3. **Online Learning**: Continuous optimization as new traces arrive
4. **Ensemble Approaches**: Combine suggestions from multiple LLMs
5. **Prompt Templates**: Reusable optimization prompts for different scenarios
6. **Hyperparameter Tuning**: Optimize LLM parameters (temperature, etc.) for better results

## Limitations

1. **LLM Dependency**: Requires working LLM API access
2. **Cost**: Each optimization cycle has API costs
3. **Latency**: Slower than pattern-based optimization
4. **Interpretability**: LLM-generated changes may be harder to understand than rule-based ones
5. **Overfitting**: Need to validate on held-out problems

## Best Practices

1. **Collect Sufficient Traces**: Need at least 5-10 traces per agent for meaningful optimization
2. **Balance Reward Weights**: Don't over-optimize for one metric at expense of others
3. **Validate Changes**: Test optimized prompts on validation set before deploying
4. **Monitor Performance**: Track metrics after optimization to ensure improvements
5. **Version Control**: Use versioned prompts to enable rollback if needed
6. **Iterative Improvement**: Run multiple optimization cycles over time

