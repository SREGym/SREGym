# How Original Prompts Are Converted to Points

## Overview

The `parse_original_prompt()` method converts a YAML prompt file into discrete `PromptPoint` objects. This process uses pattern matching (regex) to identify individual instructions/guidelines.

## Conversion Process

### Step 1: Extract System Prompt

```python
system_prompt = prompt.get("system", "")
```

Takes the `system` field from the YAML prompt file.

### Step 2: Extract Sections

```python
sections = self._extract_sections(system_prompt)
```

Splits the prompt into sections based on markdown headers (`## Title`).

**Example:**
```
Input:
  ## Workloads
  - Pod: ...
  - Deployment: ...
  
  ## Networking
  - Service: ...

Output:
  {
    "main": "...content before first section...",
    "workloads": "- Pod: ...\n- Deployment: ...",
    "networking": "- Service: ..."
  }
```

### Step 3: Parse Each Section

For each section, `_parse_section()` tries **three methods in order**:

#### Method 1: Bullet Points (Priority 1)

**Pattern:** `r"^[-*•]\s+(.+)$"`

Looks for lines starting with `-`, `*`, or `•` followed by content.

**Example:**
```
Input:
  - Review parameters carefully before calling this tool
  - Consider alternative approaches if this tool fails
  - Add error handling and validation

Output: 3 points
  Point 1: "Review parameters carefully before calling this tool"
  Point 2: "Consider alternative approaches if this tool fails"
  Point 3: "Add error handling and validation"
```

#### Method 2: Numbered Lists (Priority 2)

**Pattern:** `r"^\d+\.\s+(.+)$"`

Looks for lines starting with numbers followed by a period.

**Example:**
```
Input:
  1. Formulate a remediation plan with a list of actionable steps
  2. Execute the plan, one step at a time
  3. Check if the plan execution worked as desired

Output: 3 points
  Point 1: "Formulate a remediation plan with a list of actionable steps"
  Point 2: "Execute the plan, one step at a time"
  Point 3: "Check if the plan execution worked as desired"
```

#### Method 3: Paragraphs (Fallback)

**Pattern:** Split by double newlines (`\n\n`)

If no bullets or numbers found, splits by paragraphs.

**Example:**
```
Input:
  Monitor and diagnose an application consisting of **MANY** microservices.
  Some or none of the microservices have faults.
  
  Get all the pods and deployments to figure out what kind of services
  are running in the cluster.

Output: 2 points
  Point 1: "Monitor and diagnose an application consisting of **MANY** microservices.\nSome or none of the microservices have faults."
  Point 2: "Get all the pods and deployments to figure out what kind of services\nare running in the cluster."
```

**Note:** Paragraphs shorter than 20 characters are skipped.

### Step 4: Create PromptPoint Objects

For each parsed item, creates a `PromptPoint`:

```python
point = PromptPoint(
    id=str(uuid.uuid4()),              # Unique ID
    content=bullet.strip(),             # The instruction text
    source="original",                  # Marked as original (not learned)
    category=self._infer_category(...), # Inferred from section/content
    priority=self._infer_priority(...), # Inferred from section/content
)
```

### Step 5: Infer Category

`_infer_category()` determines the point category:

```python
if "tool" in section_title or "tool" in content:
    return "tool_usage"
elif "workflow" in section_title or "step" in section_title:
    return "workflow"
elif "warning" in section_title or "avoid" in content or "don't" in content:
    return "warning"
elif "example" in section_title:
    return "example"
elif "reference" in section_title or "kubernetes" in content:
    return "reference"
else:
    return "general"
```

### Step 6: Infer Priority

`_infer_priority()` determines the priority (1-10):

```python
if "critical" in content or "must" in content or "required" in content:
    return 9
elif "important" in content or "should" in content:
    return 7
elif "warning" in section_title or "avoid" in content:
    return 8
elif "example" in section_title:
    return 3
elif "reference" in section_title:
    return 2
else:
    return 5  # Default
```

### Step 7: Deduplicate and Save

- Checks for duplicate content (exact match)
- Only adds new points
- Saves to JSON file: `meta_agent/point_prompts/{agent_type}_points.json`

## Complete Example

### Input Prompt (YAML):

```yaml
system: |
  Monitor and diagnose an application.
  
  ## Tool Usage
  - Review parameters carefully before calling this tool
  - Consider alternative approaches if this tool fails
  
  ## Workflow
  1. Formulate a remediation plan
  2. Execute the plan step by step
  
  ## Warnings
  Avoid using get_metrics as it has low success rate.
```

### Conversion Process:

1. **Extract sections:**
   - `main`: "Monitor and diagnose an application."
   - `tool usage`: "- Review parameters...\n- Consider alternative..."
   - `workflow`: "1. Formulate...\n2. Execute..."
   - `warnings`: "Avoid using get_metrics..."

2. **Parse sections:**
   - `main`: Paragraph → 1 point
   - `tool usage`: Bullets → 2 points
   - `workflow`: Numbered → 2 points
   - `warnings`: Paragraph → 1 point

3. **Create points:**

```json
[
  {
    "id": "uuid-1",
    "content": "Monitor and diagnose an application.",
    "source": "original",
    "category": "general",
    "priority": 5
  },
  {
    "id": "uuid-2",
    "content": "Review parameters carefully before calling this tool",
    "source": "original",
    "category": "tool_usage",
    "priority": 8
  },
  {
    "id": "uuid-3",
    "content": "Consider alternative approaches if this tool fails",
    "source": "original",
    "category": "tool_usage",
    "priority": 8
  },
  {
    "id": "uuid-4",
    "content": "Formulate a remediation plan",
    "source": "original",
    "category": "workflow",
    "priority": 5
  },
  {
    "id": "uuid-5",
    "content": "Execute the plan step by step",
    "source": "original",
    "category": "workflow",
    "priority": 5
  },
  {
    "id": "uuid-6",
    "content": "Avoid using get_metrics as it has low success rate.",
    "source": "original",
    "category": "warning",
    "priority": 8
  }
]
```

## Key Characteristics

### Pattern Matching Order:
1. **Bullet points** (`-`, `*`, `•`) - Most specific
2. **Numbered lists** (`1.`, `2.`, etc.) - Specific
3. **Paragraphs** (`\n\n`) - Fallback, least specific

### Category Inference:
- Based on section title AND content
- Keywords: "tool", "workflow", "warning", "example", "reference"

### Priority Inference:
- Based on section title AND content
- Keywords: "critical", "must", "important", "should", "avoid"

### Deduplication:
- Checks exact content match before adding
- Prevents duplicate points

## Limitations

1. **No semantic understanding**: Uses regex patterns, not LLM
2. **Format-dependent**: Works best with structured formats (bullets, numbers)
3. **Paragraph splitting**: May split long paragraphs incorrectly
4. **Category inference**: Simple keyword matching, not semantic

## When Conversion Happens

- **First time**: When `_initialize_point_based_system()` is called
- **Only once**: `parse_original_prompt()` checks if original points already exist
- **Per agent type**: Each agent type (diagnosis, localization, etc.) is parsed separately


