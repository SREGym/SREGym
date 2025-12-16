# SREGym Learning Process - Detailed Workflow

## Overview
This document describes the complete learning workflow, including both LLM and non-LLM components.

## Main Learning Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    ROUND INITIALIZATION                                  │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Create LLMMetaAgent                 │
        │  - Initialize GuidelineGenerator     │
        │  - Initialize PointBasedPromptManager│
        │  - Load existing points (if any)     │
        └─────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Load Prompts from Previous Round    │
        │  (if Round > 1)                      │
        │  - Extract learned insights          │
        │  - Preserve verification stats       │
        └─────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    PROBLEM EXECUTION PHASE                              │
└─────────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
        ▼                                           ▼
┌──────────────────┐                      ┌──────────────────┐
│  Problem 1       │                      │  Problem N       │
│  Execution       │                      │  Execution       │
└──────────────────┘                      └──────────────────┘
        │                                           │
        ▼                                           ▼
┌──────────────────────────────────────────────────────────┐
│  For Each Problem:                                        │
│  1. Start Trace Collection                                │
│     - Create trace_id for each agent type                 │
│     - Record problem context                              │
│                                                            │
│  2. Execute Agent Stages                                  │
│     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│     │  Diagnosis   │→ │ Localization │→ │  Mitigation  │ │
│     └──────────────┘  └──────────────┘  └──────────────┘ │
│            │                  │                  │        │
│            ▼                  ▼                  ▼        │
│     ┌──────────────────────────────────────────────┐    │
│     │  Trace Tool Calls & Thinking Steps           │    │
│     │  - Tool name, arguments, success, duration   │    │
│     │  - Thinking/reasoning steps                  │    │
│     └──────────────────────────────────────────────┘    │
│                                                            │
│  3. End Trace                                            │
│     - Record success/failure                             │
│     - Save trace to disk                                 │
│     - Store trace_id for later validation                │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    POINT VALIDATION PHASE                               │
│                    (After ALL Problems Complete)                        │
└─────────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
        ▼                                           ▼
┌──────────────────┐                      ┌──────────────────┐
│  Problem 1       │                      │  Problem N       │
│  Validation      │                      │  Validation      │
└──────────────────┘                      └──────────────────┘
        │                                           │
        ▼                                           ▼
┌──────────────────────────────────────────────────────────┐
│  For Each Problem's Traces:                              │
│                                                            │
│  1. Identify Used Points                                  │
│     ┌──────────────────────────────────────────────┐    │
│     │  NON-LLM: Heuristic Matching                 │    │
│     │  - Tool name matching                        │    │
│     │  - Keyword overlap (3+ keywords)             │    │
│     │  - Semantic matching for workflow points     │    │
│     └──────────────────────────────────────────────┘    │
│                    │                                      │
│                    ▼                                      │
│     ┌──────────────────────────────────────────────┐    │
│     │  LLM: Usage Detection (if enabled)           │    │
│     │  - For ambiguous points                       │    │
│     │  - Batch process (8 points per call)         │    │
│     │  - Rate limited (2s between calls)           │    │
│     │  - Retry logic for rate limits               │    │
│     └──────────────────────────────────────────────┘    │
│                                                            │
│  2. Validate Points                                       │
│     ┌──────────────────────────────────────────────┐    │
│     │  For each used point:                         │    │
│     │                                                │    │
│     │  IF point is tool-related:                    │    │
│     │    → Use TOOL-LEVEL success                   │    │
│     │      (Did the tool call succeed?)             │    │
│     │                                                │    │
│     │  ELSE (non-tool point):                       │    │
│     │    → Use STAGE-LEVEL success                  │    │
│     │      (Did the stage succeed?)                 │    │
│     │                                                │    │
│     │  Update point stats:                          │    │
│     │  - verification_count++                       │    │
│     │  - success_count++ or failure_count++         │    │
│     │                                                │    │
│     │  Auto-verify if:                              │    │
│     │  - verification_count >= 3                    │    │
│     │  - success_count >= 2                         │    │
│     │                                                │    │
│     │  Auto-deactivate if:                         │    │
│     │  - Consistently failing                       │    │
│     └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    LLM OPTIMIZATION PHASE                                │
│                    (After Point Validation)                              │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Check Prerequisites                 │
        │  - API key available?                │
        │  - Enough traces? (min: 5)           │
        └─────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Load All Traces from Current Round  │
        │  (include_historical=False)          │
        └─────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    PATTERN ANALYSIS (NON-LLM)                            │
└─────────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
        ▼                                           ▼
┌──────────────────┐                      ┌──────────────────┐
│  PatternAnalyzer │                      │  Pattern Types    │
│  (Non-LLM)       │                      │  - Success        │
└──────────────────┘                      │  - Failure        │
        │                                  │  - Tool           │
        ▼                                  │  - Thinking       │
┌──────────────────────────────────────────┘  │  - Performance  │
│  Analyze Traces:                            └─────────────────┘
│  - Success patterns                         
│  - Failure patterns                         
│  - Tool effectiveness                       
│  - Thinking patterns                        
│  - Performance patterns                     
│                                              
│  Output: List of Pattern objects            
│  - pattern_type                             
│  - confidence                               
│  - recommendations                          
└──────────────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Filter High-Confidence Patterns     │
        │  (confidence >= threshold)           │
        └─────────────────────────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Generate Guidelines from Patterns   │
        │  (NON-LLM: Pattern → Insights)       │
        │  - Convert patterns to insights      │
        │  - Add to learned_insights           │
        │  - Convert to points (if enabled)   │
        └─────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    LLM-BASED OPTIMIZATION                                │
└─────────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
        ▼                                           ▼
┌──────────────────┐                      ┌──────────────────┐
│  Group Traces    │                      │  For Each Agent   │
│  by Agent Type   │                      │  Type:            │
└──────────────────┘                      └──────────────────┘
        │                                           │
        ▼                                           ▼
┌──────────────────────────────────────────────────────────┐
│  LLM Prompt Optimization:                                │
│                                                            │
│  1. Analyze Traces (NON-LLM)                             │
│     ┌──────────────────────────────────────────────┐    │
│     │  - Calculate success rate                     │    │
│     │  - Calculate avg latency                     │    │
│     │  - Count tool usage                          │    │
│     │  - Extract ground truth info                 │    │
│     │  - Identify failure patterns                 │    │
│     └──────────────────────────────────────────────┘    │
│                                                            │
│  2. Build LLM Prompt                                      │
│     ┌──────────────────────────────────────────────┐    │
│     │  Include:                                     │    │
│     │  - Current prompt                             │    │
│     │  - Trace analysis results                     │    │
│     │  - Reward specification                       │    │
│     │  - Existing insights (for deduplication)      │    │
│     │  - Ground truth examples                      │    │
│     └──────────────────────────────────────────────┘    │
│                                                            │
│  3. Call LLM (LLM Component)                             │
│     ┌──────────────────────────────────────────────┐    │
│     │  Model: gemini/gemini-2.5-flash               │    │
│     │  Task: Generate NEW insights to ADD           │    │
│     │  - Must not duplicate existing insights       │    │
│     │  - Must preserve original prompt              │    │
│     │  - Focus on improving success/latency          │    │
│     └──────────────────────────────────────────────┘    │
│                    │                                      │
│                    ▼                                      │
│     ┌──────────────────────────────────────────────┐    │
│     │  LLM Response:                                │    │
│     │  {                                            │    │
│     │    "new_insights": [                          │    │
│     │      {                                        │    │
│     │        "type": "recommendation",              │    │
│     │        "content": "...",                      │    │
│     │        "reasoning": "..."                     │    │
│     │      }                                        │    │
│     │    ]                                          │    │
│     │  }                                            │    │
│     └──────────────────────────────────────────────┘    │
│                                                            │
│  4. Process LLM Response                                 │
│     ┌──────────────────────────────────────────────┐    │
│     │  - Deduplicate against existing insights      │    │
│     │  - Add to learned_insights                    │    │
│     │  - Convert to points (if point-based)         │    │
│     │  - Resolve conflicts                          │    │
│     └──────────────────────────────────────────────┘    │
│                                                            │
│  5. Rebuild Prompts                                       │
│     ┌──────────────────────────────────────────────┐    │
│     │  - Combine original + learned insights        │    │
│     │  - Or rebuild from points (if point-based)    │    │
│     │  - Save versioned prompts                     │    │
│     └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    SAVE RESULTS                                          │
└─────────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
        ▼                                           ▼
┌──────────────────┐                      ┌──────────────────┐
│  Save Prompts    │                      │  Save Results    │
│  - Active prompts│                      │  - Problem results│
│  - Versioned     │                      │  - Learning result│
│  - Configs       │                      │  - Round info     │
└──────────────────┘                      └──────────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────┐
        │  Round Complete                      │
        │  - Mark status: "completed"          │
        │  - Save round info                   │
        └─────────────────────────────────────┘
```

## Component Details

### 1. Point-Based Prompt System

```
┌─────────────────────────────────────────────────────────┐
│  Point Lifecycle                                         │
└─────────────────────────────────────────────────────────┘

Original Prompt
      │
      ▼
┌─────────────────┐
│  Parse into     │  ← parse_original_prompt()
│  Points         │     (NON-LLM: regex/parsing)
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  Store Points   │
│  - source: "original" │
│  - category, priority │
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  Agent Uses     │
│  Points in      │
│  Execution      │
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  Identify Used  │
│  Points         │
│  ┌───────────┐  │
│  │ Heuristic │  │  ← NON-LLM
│  │ Matching  │  │
│  └───────────┘  │
│  ┌───────────┐  │
│  │ LLM       │  │  ← LLM (if ambiguous)
│  │ Detection │  │
│  └───────────┘  │
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  Validate       │
│  Points         │
│  - Tool-level   │  ← For tool points
│  - Stage-level  │  ← For other points
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  Update Stats   │
│  - verification │
│  - success/     │
│    failure      │
└─────────────────┘
      │
      ▼
┌─────────────────┐
│  Auto Actions   │
│  - Verify       │  ← If 3+ verifications, 2+ successes
│  - Deactivate   │  ← If consistently failing
└─────────────────┘
```

### 2. LLM vs Non-LLM Components

#### NON-LLM Components:
- **PatternAnalyzer**: Analyzes traces to find patterns
- **Heuristic Point Matching**: Tool name matching, keyword overlap
- **Point Validation**: Updates stats based on success/failure
- **Trace Analysis**: Calculates metrics (success rate, latency, etc.)
- **Pattern → Insight Conversion**: Converts patterns to insights

#### LLM Components:
- **Point Usage Detection**: Identifies which points were used (for ambiguous points)
- **Prompt Optimization**: Generates new insights based on trace analysis
- **Conflict Detection**: Detects conflicting points (if enabled)
- **Insight Deduplication**: Checks for semantic duplicates

### 3. Data Flow

```
Traces (from execution)
    │
    ├─→ PatternAnalyzer (NON-LLM)
    │       │
    │       └─→ Patterns
    │               │
    │               └─→ GuidelineGenerator.generate_guidelines()
    │                       │
    │                       └─→ Pattern-based Insights
    │
    └─→ LLMPromptOptimizer (LLM)
            │
            ├─→ Trace Analysis (NON-LLM)
            │       │
            │       └─→ Metrics, patterns, ground truth
            │
            └─→ LLM Call
                    │
                    └─→ New Insights (JSON)
                            │
                            └─→ Deduplication (NON-LLM)
                                    │
                                    └─→ Learned Insights
                                            │
                                            └─→ Points (if point-based)
                                                    │
                                                    └─→ Rebuild Prompts
```

### 4. Key Decision Points

```
┌─────────────────────────────────────────┐
│  When to Use LLM vs Non-LLM             │
└─────────────────────────────────────────┘

Point Usage Detection:
  └─→ Try Heuristic First (NON-LLM)
      └─→ If ambiguous → Use LLM

Pattern Analysis:
  └─→ Always NON-LLM (PatternAnalyzer)

Insight Generation:
  ├─→ Pattern-based: NON-LLM
  └─→ LLM Optimization: LLM (if enabled & enough traces)

Conflict Detection:
  └─→ Can use LLM (if enabled) or NON-LLM heuristics
```

## Summary

**NON-LLM Components:**
- Pattern analysis
- Heuristic point matching
- Point validation
- Trace metrics calculation
- Pattern-to-insight conversion
- Deduplication (simple checks)

**LLM Components:**
- Point usage detection (for ambiguous points)
- Prompt optimization (generating new insights)
- Conflict detection (optional)
- Semantic deduplication (optional)

**Flow:**
1. Execute problems → Collect traces
2. Validate points (heuristic + optional LLM)
3. Analyze patterns (NON-LLM)
4. Generate pattern-based insights (NON-LLM)
5. Optimize with LLM (if enabled)
6. Apply insights → Rebuild prompts
7. Save results


