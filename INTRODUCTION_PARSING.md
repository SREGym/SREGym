# How Introduction/Task Description is Treated

## Overview

The introduction and task description (everything **before** the first `##` section header) is treated as a special section called **"main"** and parsed using the same methods as other sections.

## Current Behavior

### 1. Section Extraction

```python
# Everything before first "##" header → "main" section
current_section = "main"  # Default section name
```

**Example:**
```
Monitor and diagnose an application...
[task description text]
[no ## headers yet]

## Workloads (Applications)  ← First header starts new section
- Pod: ...
```

**Result:** All text before `## Workloads` becomes the "main" section.

### 2. Parsing Method

Since introductions typically don't have bullet points (`-`) or numbered lists (`1.`), the parser **falls back to paragraph splitting**:

```python
# Try bullet points → No match
# Try numbered list → No match
# Fall back to paragraphs
paragraphs = content.split("\n\n")  # Split by double newlines
```

### 3. Current Issue

**Problem:** If the introduction doesn't have double newlines (`\n\n`), the **entire introduction becomes one large point**.

**Example from actual diagnosis agent:**
- The entire introduction (1565 characters) was parsed as **one single point**
- Contains: task description, instructions, and even some section headers
- This makes it hard to validate and track individual instructions

**Actual Point Content:**
```
"Monitor and diagnose an application consisting of **MANY** microservices. 
Some or none of the microservices have faults. Get all the pods and 
deployments to figure out what kind of services are running in the cluster.  
Carefully identify the whether the faults are present and if they are, and 
identify what is the root cause of the fault.\nStop diagnosis once you've 
found the root cause of the faults.\nGo as deep as you can into what is 
causing the issue.\nYour instructions to the tools must be clear and concise. 
Your queries to tools need to be single turn.\nRemember to check these, and 
remember this information: ## Workloads (Applications) - **Pod**: ..."
```

### 4. Category and Priority Inference

For the "main" section:
- **Category:** Usually inferred as `"tool_usage"` if content contains "tool", otherwise `"general"`
- **Priority:** Default `5` (medium priority), unless keywords like "critical" or "must" are found

## Recommendations

### Option 1: Split by Single Newlines (More Granular)

If the introduction has single newlines (`\n`), split by those instead of requiring double newlines:

```python
# For "main" section, be more aggressive with splitting
if section_title == "main":
    # Split by single newlines for task descriptions
    sentences = content.split("\n")
    for sentence in sentences:
        if len(sentence.strip()) >= 20:
            # Create point from sentence
```

### Option 2: Split by Sentence Boundaries

Use sentence boundaries (`.`, `!`, `?`) to split the introduction:

```python
import re
sentences = re.split(r'[.!?]+\s+', content)
for sentence in sentences:
    if len(sentence.strip()) >= 20:
        # Create point
```

### Option 3: Manual Annotation

Add special markers in the original prompt to indicate where points should be split:

```yaml
system: |
  <!-- POINT: Task Overview -->
  Monitor and diagnose an application...
  
  <!-- POINT: Instructions -->
  Your instructions to the tools must be clear...
```

## Current Workaround

The system currently works, but the introduction point is:
- **Too large** (1565 characters)
- **Hard to validate** (contains multiple instructions)
- **Low granularity** (can't track which specific instruction helped)

## Summary

| Aspect | Current Behavior |
|--------|------------------|
| **Section Name** | `"main"` |
| **Parsing Method** | Paragraph splitting (by `\n\n`) |
| **Issue** | Entire introduction becomes one point if no `\n\n` |
| **Category** | `tool_usage` or `general` (inferred) |
| **Priority** | `5` (default) |
| **Validation** | Entire point validated as one unit |


