# DiagnosisJudge v3.0 — Changelog

## Overview

This document describes the improvements made to the `DiagnosisJudge` evaluation
system under `sregym/conductor/oracles/llm_as_a_judge/`. The changes address three
shortcomings of the v2.0 judge:

1. The judge relied on a short `root_cause` string as ground truth, with no access
   to the structured fault-injection logic that produced the fault.
2. The judge only evaluated the agent's *answer* — not *how* the agent arrived at it.
3. The judge prompt was terse, with no chain-of-thought guidance or worked examples
   for the evaluator LLM.

All changes are scoped to files inside `sregym/conductor/oracles/llm_as_a_judge/`.
No other files in the repository are modified. Backward compatibility is preserved:
existing callers that use `judge(solution, expectation)` or `evaluate(solution)`
continue to work without changes.

---

## 1. Fault-Injection-Aware Ground Truth

**Problem.** The v2.0 judge received only `problem.root_cause` — a one-sentence
natural-language string like *"The deployment checkout has the environment variable
PRODUCT_CATALOG_ADDR configured with an incorrect port 8082 instead of 8080."*
This forced the judge LLM to do all the semantic matching from a single sentence,
with no structured data to anchor its comparison.

**Solution.** A new module `fault_spec_extractor.py` introspects the live `Problem`
object (without touching the cluster or network) and builds a `FaultSpec` dataclass:

| Field | Example | Source |
|---|---|---|
| `fault_category` | `misconfiguration` | Taxonomy lookup from class name |
| `fault_mechanism` | `incorrect_port_assignment` | Class-to-mechanism map |
| `target_component` | `checkout` | `problem.faulty_service` |
| `target_resource_kind` | `Deployment` | Inferred from `root_cause` text |
| `namespace` | `astronomy-shop` | `problem.namespace` |
| `parameters` | `{env_var: PRODUCT_CATALOG_ADDR, incorrect_port: 8082, correct_port: 8080}` | Attribute scan on problem |
| `injector_class` | `ApplicationFaultInjector` | `problem.injector` type |
| `injector_method` | `inject_incorrect_port_assignment` | Source-code inspection of `inject_fault` |
| `problem_class` | `IncorrectPortAssignment` | `type(problem).__name__` |

**Coverage.** The `_CLASS_TO_MECHANISM` map covers all ~60 Problem subclasses
registered in `ProblemRegistry`. The `_FAULT_TAXONOMY` maps ~50 fault mechanisms
to categories. Unknown classes fall back gracefully to
snake_case conversion of the class name and `category=unknown`.

**How it's used.** When `DiagnosisJudge.judge_detailed()` is called with a
`problem` argument, it calls `extract_fault_spec(problem)` and renders the
structured spec into the judge prompt as a markdown section. The natural-language
`root_cause` is still included as a fallback. The checklist dimension definitions
and evaluator hints now reference the structured fields (e.g., *"Compare against
`fault_category` in the structured spec"*).

### Files changed

- **New:** `fault_spec_extractor.py`
- **Modified:** `judge.py` — `_build_user_message()` calls the extractor
- **Modified:** `llm_as_a_judge_oracle.py` — passes `self.problem` through
- **Modified:** `__init__.py` — exports `FaultSpec`, `extract_fault_spec`

---

## 2. Reasoning Trajectory Coherence (D4)

**Problem.** The v2.0 judge evaluated only the final diagnosis text against the
ground truth. An agent could arrive at the correct answer by luck or hallucination
— there was no check on whether its investigation process was sound.

**Solution.** A new evaluation dimension **D4 — Reasoning Trajectory Coherence**
is added to `rca_checklists.yaml`. It has three questions:

| ID | Question | What it checks |
|---|---|---|
| D4-Q1 | Did the agent's tool calls investigate the component it ultimately blames? | Trace coverage: the agent actually *looked at* what it diagnosed |
| D4-Q2 | Is the final diagnosis logically supported by observations in the trace? | Evidence grounding: error messages, status conditions, or metrics in the trace entail the conclusion |
| D4-Q3 | Is the reasoning free of major logical jumps or hallucinated evidence? | Coherence: no skipped steps, no phantom data cited |

**Scoring.** Weights are rebalanced to four equal dimensions at 0.25 each
(previously D1/D2/D3 at 0.33/0.33/0.34). The threshold remains 0.70.

**Trace input.** The `evaluate()` method on `LLMAsAJudgeOracle` now accepts an
optional `trace` parameter. When provided, it's included in the judge prompt under
an "Agent Reasoning Trace" section. Long traces are truncated to ~12K characters
(keeping head and tail) to stay within context limits. When no trace is provided,
the prompt instructs the judge to answer all D4 questions as "No" with evidence
"no trace provided" — so existing callers that don't pass traces see D4 default
to zero without errors.

### Files changed

- **Modified:** `rca_checklists.yaml` — added D4 dimension, rebalanced weights, bumped to v3.0
- **Modified:** `judge.py` — `judge()` and `judge_detailed()` accept `agent_trace` kwarg; `_build_user_message()` renders trace section
- **Modified:** `llm_as_a_judge_oracle.py` — `evaluate(solution, trace=None, duration=None)` passes trace through
- **Unchanged:** `models.py` — already generic enough for any number of dimensions

---

## 3. Chain-of-Thought Prompting and In-Context Learning

**Problem.** The v2.0 system prompt was a short bulleted list of rules with no
reasoning guidance. The judge LLM had to figure out *how* to evaluate on its own,
leading to inconsistent scoring and shallow evidence extraction.

**Solution.** The system prompt in `DiagnosisJudge._SYSTEM_PROMPT_TEMPLATE` is
rewritten with three major additions:

### 3a. Explicit Chain-of-Thought Protocol

The judge is instructed to follow four steps *per dimension* before answering:

1. **Understand the dimension** — re-read the definition and hints.
2. **Gather evidence** — scan the diagnosis (D1–D3) or trace (D4).
3. **Compare against ground truth** — use the *structured fault spec* as the
   authoritative reference, with specific field callouts per dimension.
4. **Answer each question** — produce the JSON object.

### 3b. In-Context Learning Examples

Five worked examples are embedded in the system prompt, one per evaluation scenario:

| Example | Dimension | Outcome | Purpose |
|---|---|---|---|
| Checkout port misconfiguration | D1 (Localization) | All Yes | Shows correct component matching |
| PRODUCT_CATALOG_ADDR env var | D2 (Characterization) | All Yes | Shows parameter-level matching |
| Frontend blamed with extra services | D3 (Scope) | Mixed | Shows over-attribution detection |
| Payment-service with matching trace | D4 (Trajectory) | All Yes | Shows trace-to-diagnosis validation |
| Cart-service blamed but trace shows frontend OOM | D4 (Trajectory) | All No | Shows trace-unsupported diagnosis detection |

Each example shows the internal reasoning chain the judge should follow, then the
expected Yes/No answers with brief justification.

### 3c. Robust JSON Parsing

The `_parse_response()` method is updated to handle cases where the model emits
chain-of-thought text before the JSON array. It now searches for the outermost
`[...]` bracket pair before attempting JSON parse. The dimension score divisor
uses `len(qs)` instead of the hardcoded `3.0`.

### Files changed

- **Modified:** `judge.py` — complete rewrite of `_SYSTEM_PROMPT_TEMPLATE`; updated `_parse_response()` with bracket extraction; dynamic divisor in scoring

---

## Backward Compatibility

All changes are backward-compatible:

| Call site | Before | After | Breaking? |
|---|---|---|---|
| `judge.judge(solution, expectation)` | Works | Still works — `problem` and `agent_trace` default to `None` | No |
| `judge.judge_detailed(solution, expectation)` | Works | Still works — same defaults | No |
| `oracle.evaluate(solution)` | Works | Still works — `trace` and `duration` default to `None` | No |
| `DiagnosisJudge(checklist_path=...)` | Loads v2.0 YAML | Loads v3.0 YAML — extra D4 dimension handled automatically | No |

When `problem=None`, the prompt omits the structured fault spec section and falls
back to the natural-language `expectation` string (same as v2.0). When
`agent_trace=None`, D4 questions all score "No" and the composite score is computed
over all four dimensions (effectively penalizing the missing trace by 25% of the
composite weight).

---

## File Inventory

| File | Status | Lines | Description |
|---|---|---|---|
| `fault_spec_extractor.py` | **New** | ~280 | Extracts structured `FaultSpec` from `Problem` objects |
| `rca_checklists.yaml` | **Modified** | ~100 | v3.0: added D4, rebalanced weights, updated hints |
| `judge.py` | **Modified** | ~560 | CoT prompt, in-context examples, fault spec + trace integration |
| `llm_as_a_judge_oracle.py` | **Modified** | ~110 | Passes `problem` and `trace` to judge |
| `__init__.py` | **Modified** | ~15 | Exports new public API |
| `models.py` | **Unchanged** | ~40 | Already generic for N dimensions |
