# SREGym Learning Method - Quick Summary

## High-Level Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    MULTI-ROUND LEARNING LOOP                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  ROUND N                             │
        │                                      │
        │  1. Load Prompts (Round 1: original, │
        │     Round 2+: accumulated)          │
        │                                      │
        │  2. Parse → Discrete Points          │
        │     - Original points (source="original") │
        │     - Learned points (source="learned")   │
        └─────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  FOR EACH PROBLEM:                  │
        │                                      │
        │  ┌──────────────────────────────┐   │
        │  │ Execute Agent                │   │
        │  │ with Current Prompts         │   │
        │  └──────────────────────────────┘   │
        │              │                       │
        │              ▼                       │
        │  ┌──────────────────────────────┐   │
        │  │ Collect Trace                 │   │
        │  │ - Tool calls                  │   │
        │  │ - Thinking steps              │   │
        │  │ - Final submission            │   │
        │  │ - Success/failure status      │   │
        │  └──────────────────────────────┘   │
        └─────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  AFTER ALL PROBLEMS:                │
        │                                      │
        │  1. Point Identification             │
        │     ┌──────────────────────────┐    │
        │     │ For each trace:          │    │
        │     │ - Heuristic matching     │    │
        │     │ - LLM analysis (if needed)│   │
        │     │ - Mark used points       │    │
        │     └──────────────────────────┘    │
        │                                      │
        │  2. Point Validation                 │
        │     ┌──────────────────────────┐    │
        │     │ Update point stats:      │    │
        │     │ - success_count          │    │
        │     │ - failure_count          │    │
        │     │ - verification_count     │    │
        │     │ - Mark verified if ready │    │
        │     └──────────────────────────┘    │
        │                                      │
        │  3. LLM Optimization (if enabled)     │
        │     ┌──────────────────────────┐    │
        │     │ - Analyze all traces     │    │
        │     │ - Calculate metrics      │    │
        │     │ - Generate new insights  │    │
        │     └──────────────────────────┘    │
        │                                      │
        │  4. Process New Insights             │
        │     ┌──────────────────────────┐    │
        │     │ - Deduplicate            │    │
        │     │ - Convert to points      │    │
        │     │ - Detect conflicts       │    │
        │     │ - Resolve conflicts      │    │
        │     └──────────────────────────┘    │
        │                                      │
        │  5. Rebuild Prompts                  │
        │     ┌──────────────────────────┐    │
        │     │ - Original points        │    │
        │     │ - Verified learned points│    │
        │     │ - Active points only    │    │
        │     │ - Format as YAML        │    │
        │     └──────────────────────────┘    │
        └─────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  SAVE RESULTS                        │
        │  - Updated prompts                   │
        │  - Point database                    │
        │  - Execution traces                  │
        │  - Learning statistics               │
        └─────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  Next Round?    │
                    └─────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                    ▼                   ▼
              ┌──────────┐      ┌──────────┐
              │   Yes    │      │    No    │
              └──────────┘      └──────────┘
                    │                   │
                    ▼                   ▼
            [Loop to Round N+1]    [End Learning]
```

## Key Components

### 1. Point-Based System
- **PromptPoint**: Discrete instruction with ID, content, stats
- **PointManager**: Manages points, detects conflicts, rebuilds prompts
- **Point Lifecycle**: Created → Used → Validated → Verified → Active/Inactive

### 2. Trace Collection
- **Tool Calls**: Captured via MCP interceptor
- **Thinking Steps**: Agent reasoning captured
- **Execution Results**: Success/failure from conductor

### 3. Point Identification
- **Heuristic-First**: Fast pattern matching (tool names, keywords)
- **LLM-Primary**: Deep analysis when heuristic is ambiguous
- **Batch Processing**: All traces analyzed together

### 4. Conflict Detection
- **Fast Checks**: Tool conflicts, semantic patterns
- **LLM Check**: For ambiguous cases
- **Resolution**: Deactivate lower-priority conflicting points

### 5. Learning Mechanisms
- **Pattern Analysis**: Non-LLM pattern extraction
- **LLM Optimization**: Generates new insights from traces
- **Validation**: Points validated based on execution success
- **Accumulation**: Insights build across rounds

## Data Flow

```
Original Prompts (YAML)
    ↓
Parse into Points
    ↓
Point Database (JSON)
    ↓
Agent Execution
    ↓
Execution Traces
    ↓
Point Identification
    ↓
Point Validation
    ↓
LLM Optimization → New Insights
    ↓
Convert to Points
    ↓
Conflict Detection & Resolution
    ↓
Rebuild Prompts from Points
    ↓
Updated Prompts (YAML)
    ↓
Next Round
```

## Round Progression

| Round | Prompts Source | Points Status |
|-------|---------------|---------------|
| Round 1 | Original only | Original points parsed |
| Round 2 | Original + Round 1 learned | Original + Round 1 points |
| Round 3 | Original + Rounds 1-2 learned | Original + Rounds 1-2 points |
| ... | ... | ... |
| Round N | Original + All previous | Accumulated verified points |

## Success Criteria

A point is marked as **verified** when:
- `verification_count >= 3`
- `success_count >= 2`

A point is **removed** when:
- `failure_count > success_count * 2`
- `verification_count >= 3` and `success_count == 0`

## Performance Optimizations

1. **Batch Processing**: All traces processed together after round
2. **Incremental Conflict Detection**: Only check new points
3. **Caching**: Conflict results cached
4. **Rate Limiting**: LLM calls rate-limited with retries
5. **Heuristic-First**: Fast checks before LLM calls

