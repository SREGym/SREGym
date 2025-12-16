# Updated Learning Workflow: Original Prompts + Learned Insights

## Overview

The system now keeps **original prompts intact** and only treats **learned insights as points**. Learned insights are appended as a separate "Learned Insights" section.

## Key Changes

1. **Original prompts are NOT converted to points** - they remain in their original YAML format
2. **Only learned insights are stored as points** - these come from trace analysis and LLM optimization
3. **When rebuilding prompts** - original prompt stays intact, learned insights are appended as a section

## Complete Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│  INITIALIZATION                                                 │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  1. Load Original Prompts           │
        │     - From YAML files                │
        │     - Preserved as-is (no parsing)  │
        │     - Stored in original_prompts    │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  2. Load Learned Points            │
        │     - From JSON files              │
        │     - Only points with             │
        │       source="learned"              │
        │     - Original points ignored      │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  3. Create Backups                 │
        │     - Backup original prompts      │
        │     - In backups/ directory        │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  System Ready                       │
        │  - Original prompts intact          │
        │  - Learned points loaded            │
        └─────────────────────────────────────┘
```

## Learning Round Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│  LEARNING ROUND                                                 │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  For Each Problem:                  │
        │  1. Load prompt (original + learned)│
        │  2. Agent executes                  │
        │  3. Collect trace                   │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  After All Problems:                │
        │  1. Analyze traces (pattern-based)  │
        │  2. LLM optimization (if enabled)   │
        │  3. Generate new insights          │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  Process New Insights               │
        │                                     │
        │  ┌───────────────────────────────┐ │
        │  │ For each new insight:         │ │
        │  │ 1. Check for duplicates       │ │
        │  │ 2. Create PromptPoint          │ │
        │  │    - source="learned"         │ │
        │  │    - category (mapped)        │ │
        │  │    - priority (default: 6)    │ │
        │  │ 3. Add to point_manager        │ │
        │  │ 4. Save to JSON               │ │
        │  └───────────────────────────────┘ │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  Conflict Detection                  │
        │  - Check conflicts between           │
        │    new learned points                │
        │  - Resolve conflicts                 │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  Rebuild Prompt                      │
        │                                     │
        │  ┌───────────────────────────────┐ │
        │  │ 1. Start with original prompt │ │
        │  │    (deep copy, unchanged)     │ │
        │  │                               │ │
        │  │ 2. Get learned points only    │ │
        │  │    (source="learned")         │ │
        │  │                               │ │
        │  │ 3. Build "Learned Insights"   │ │
        │  │    section:                   │ │
        │  │    - Group by category        │ │
        │  │    - Sort by verified/priority│ │
        │  │    - Format with markers       │ │
        │  │      (✅ VERIFIED / ⚠️ UNVERIFIED)│ │
        │  │                               │ │
        │  │ 4. Append to original prompt  │ │
        │  │    (preserves original)       │ │
        │  └───────────────────────────────┘ │
        └─────────────────────────────────────┘
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  Save Updated Prompt                  │
        │  - Save to main config file          │
        │  - Save versioned copy               │
        └─────────────────────────────────────┘
```

## Prompt Structure

### Before Learning (Original Prompt)
```yaml
system: >
  Monitor and diagnose an application...
  
  ## Workloads (Applications)
  - **Pod**: ...
  - **Deployment**: ...
  
  ## Networking
  - **Service**: ...
```

### After Learning (With Learned Insights)
```yaml
system: >
  Monitor and diagnose an application...
  
  ## Workloads (Applications)
  - **Pod**: ...
  - **Deployment**: ...
  
  ## Networking
  - **Service**: ...
  
  ## Learned Insights (Additive - Original Content Preserved Above)
  The following insights have been learned from past executions. Original prompt content is preserved above.
  
  ### Tool Usage Guidelines
  
  ✅ VERIFIED
  When using get_metrics, always check for empty results and consider alternative approaches.
  
  ⚠️ UNVERIFIED (being tested)
  Consider using get_traces before get_metrics for better context.
  
  ### Workflow Guidelines
  
  ✅ VERIFIED
  Formulate a plan before executing tool calls.
```

## Point Lifecycle

```
┌─────────────────────────────────────────────────────────┐
│  Learned Insight Lifecycle                              │
└─────────────────────────────────────────────────────────┘

New Insight from Trace Analysis/LLM
        │
        ▼
┌──────────────────────┐
│  Check Duplicates    │
│  (by content)        │
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Create PromptPoint  │
│  - source="learned"  │
│  - category (mapped) │
│  - priority: 6       │
│  - verified: false   │
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Save to JSON        │
│  (point_prompts/)    │
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Conflict Detection  │
│  (with other points) │
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Used in Execution   │
│  (identified in trace)│
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Validation          │
│  - success_count++    │
│  - failure_count++   │
│  - verification_count++│
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Mark Verified?      │
│  (if 3 uses, 2+ success)│
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Active in Prompt    │
│  (rebuild includes)   │
└──────────────────────┘
```

## Code Flow

### Initialization
```python
# In GuidelineGenerator.__init__()
1. Load original prompts from YAML → original_prompts
2. Initialize PointBasedPromptManager
3. Load learned points from JSON (source="learned" only)
4. Skip parsing original prompts into points
```

### Adding New Insights
```python
# In _apply_changes_to_template()
1. For each new insight:
   - Check duplicates
   - point = point_manager.add_learned_insight(insight)
   - point.source = "learned"  # Always
   - Save to JSON

2. Detect conflicts (only between learned points)
3. Resolve conflicts
4. Rebuild prompt
```

### Rebuilding Prompt
```python
# In _rebuild_prompt_from_points()
1. Start with original_prompts[agent_type] (deep copy)
2. Get learned points: [p for p in points if p.source == "learned"]
3. Build "Learned Insights" section:
   - Group by category
   - Sort by verified, priority
   - Format with markers
4. Append to original system prompt
5. Save updated template
```

## Key Differences from Old System

| Aspect | Old System | New System |
|--------|-----------|------------|
| **Original Prompts** | Parsed into points | Kept intact |
| **Points Source** | Original + Learned | Learned only |
| **Prompt Structure** | Rebuilt from all points | Original + Learned section |
| **Point Validation** | All points validated | Only learned points validated |
| **Conflict Detection** | All points checked | Only learned points checked |

## Benefits

1. **Original prompts preserved** - No risk of losing original formatting/structure
2. **Clear separation** - Original content vs. learned insights
3. **Easier to review** - Can see what was original vs. what was learned
4. **Simpler validation** - Only learned insights need validation
5. **Better maintainability** - Original prompts remain human-readable

## Example: Adding a New Insight

```
1. Trace Analysis finds pattern:
   "Using get_metrics before get_traces improves success rate"

2. Create insight:
   {
     "type": "add_guidance",
     "content": "Consider using get_traces before get_metrics for better context",
     "pattern": "Using get_metrics before get_traces improves success rate"
   }

3. Add as point:
   - source="learned"
   - category="tool_usage" (mapped from "add_guidance")
   - priority=6
   - verified=false

4. Save to JSON:
   meta_agent/point_prompts/diagnosis_points.json

5. Rebuild prompt:
   - Original prompt: unchanged
   - Append: "## Learned Insights ... ✅ VERIFIED ..."

6. Agent uses updated prompt:
   - Original instructions: preserved
   - Learned insights: appended at end
```

