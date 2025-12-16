# SREGym Learning Method - Visual Diagram

## Overview Diagram

```mermaid
graph TB
    Start([Start Learning]) --> RoundInit[Round Initialization]
    
    RoundInit --> LoadPrompts{Load Previous<br/>Round Prompts?}
    LoadPrompts -->|Round 1| OriginalPrompts[Original Prompts]
    LoadPrompts -->|Round 2+| LoadedPrompts[Loaded Prompts<br/>with Accumulated Points]
    
    OriginalPrompts --> ParsePoints[Parse Prompts into<br/>Discrete Points]
    LoadedPrompts --> ParsePoints
    
    ParsePoints --> InitMetaAgent[Initialize Meta-Agent<br/>- PointManager<br/>- TraceCollector<br/>- GuidelineGenerator]
    
    InitMetaAgent --> ProblemLoop[For Each Problem]
    
    ProblemLoop --> ExecAgent[Execute Agent<br/>with Current Prompts]
    ExecAgent --> CollectTrace[Collect Execution Trace<br/>- Tool Calls<br/>- Thinking Steps<br/>- Final Submission]
    
    CollectTrace --> WaitGrading[Wait for Grading]
    WaitGrading --> StoreTrace[Store Trace with<br/>Success/Failure Status]
    
    StoreTrace --> MoreProblems{More Problems?}
    MoreProblems -->|Yes| ProblemLoop
    MoreProblems -->|No| BatchValidation
    
    BatchValidation[Batch Process All Traces<br/>Identify Used Points]
    
    BatchValidation --> PointID[Point Identification]
    
    PointID --> HeuristicCheck{Heuristic<br/>Match?}
    HeuristicCheck -->|Yes| MarkUsed[Mark Point as Used]
    HeuristicCheck -->|No| LLMCheck[LLM Analysis<br/>Check Point Usage]
    LLMCheck --> MarkUsed
    
    MarkUsed --> ValidatePoints[Validate Points<br/>Update Stats:<br/>- success_count<br/>- failure_count<br/>- verification_count]
    
    ValidatePoints --> LLMOptimization{LLM Optimization<br/>Enabled?}
    
    LLMOptimization -->|Yes| AnalyzeTraces[Analyze All Traces<br/>- Calculate Metrics<br/>- Extract Patterns<br/>- Compare with Ground Truth]
    LLMOptimization -->|No| PatternAnalysis
    
    AnalyzeTraces --> LLMGenerate[LLM Generates<br/>New Insights]
    LLMGenerate --> PatternAnalysis[Pattern-Based<br/>Insight Generation]
    
    PatternAnalysis --> Deduplicate[Deduplicate Insights]
    
    Deduplicate --> ConvertToPoints[Convert Insights<br/>to PromptPoints<br/>source='learned']
    
    ConvertToPoints --> ConflictDetect[Conflict Detection]
    
    ConflictDetect --> FastCheck{Fast Checks<br/>- Tool conflicts<br/>- Semantic patterns}
    FastCheck -->|Conflict Found| MarkConflict[Mark Conflicts]
    FastCheck -->|Ambiguous| LLMConflict[LLM Conflict Check]
    LLMConflict --> MarkConflict
    
    MarkConflict --> ResolveConflicts[Resolve Conflicts<br/>- Deactivate conflicting points<br/>- Keep higher priority]
    
    ResolveConflicts --> RebuildPrompt[Rebuild Prompt from Points<br/>1. Original points<br/>2. Verified learned points<br/>3. Active points only]
    
    RebuildPrompt --> SaveResults[Save Results<br/>- Updated prompts<br/>- Point database<br/>- Traces<br/>- Learning stats]
    
    SaveResults --> NextRound{More Rounds?}
    NextRound -->|Yes| RoundInit
    NextRound -->|No| End([End Learning])
    
    style Start fill:#90EE90
    style End fill:#FFB6C1
    style LLMCheck fill:#FFE4B5
    style LLMGenerate fill:#FFE4B5
    style LLMConflict fill:#FFE4B5
    style BatchValidation fill:#E6E6FA
    style ConflictDetect fill:#F0E68C
    style RebuildPrompt fill:#DDA0DD
```

## Detailed Component Flow

### 1. Point-Based System Architecture

```mermaid
graph LR
    A[Original Prompt<br/>YAML File] --> B[Parse into Points<br/>Regex/Pattern Matching]
    B --> C[PromptPoint Objects<br/>- id, content, source<br/>- category, priority<br/>- verification stats]
    
    C --> D[Point Storage<br/>JSON Database]
    
    E[Execution Traces] --> F[Point Identification]
    F --> G[Update Point Stats]
    G --> D
    
    H[LLM Insights] --> I[Convert to Points]
    I --> J[Conflict Detection]
    J --> K[Resolve Conflicts]
    K --> D
    
    D --> L[Rebuild Prompt<br/>from Active Points]
    L --> M[Final Prompt<br/>for Agent]
    
    style B fill:#E6E6FA
    style F fill:#FFE4B5
    style J fill:#F0E68C
    style L fill:#DDA0DD
```

### 2. Point Identification Process

```mermaid
graph TD
    A[Execution Trace] --> B[Extract Summary<br/>- Tool calls<br/>- Reasoning steps<br/>- Actions taken]
    
    B --> C[Get All Active Points<br/>for Agent Type]
    
    C --> D{Identification Mode}
    
    D -->|Heuristic-First| E[Pattern Matching<br/>- Tool name matching<br/>- Keyword matching<br/>- Workflow pattern matching]
    D -->|LLM-Primary| F[LLM Analysis<br/>Batch Processing]
    
    E --> G{Match Found?}
    G -->|Yes| H[Mark as Used]
    G -->|No/Ambiguous| F
    
    F --> I[LLM Prompt:<br/>Analyze trace and<br/>determine which points<br/>were actually used]
    I --> J[LLM Response<br/>JSON: used_points]
    J --> H
    
    H --> K[Update Point Statistics]
    
    style E fill:#E6E6FA
    style F fill:#FFE4B5
    style I fill:#FFE4B5
```

### 3. Conflict Detection & Resolution

```mermaid
graph TD
    A[New Learned Points] --> B[Conflict Detection]
    
    B --> C{Fast Checks}
    
    C --> D[Tool Usage Conflict<br/>Same tool,<br/>opposite instructions]
    C --> E[Semantic Pattern<br/>Contradictory content]
    C --> F[Category Check<br/>Different categories<br/>unlikely to conflict]
    
    D --> G{Conflict?}
    E --> G
    F --> G
    
    G -->|Yes| H[Mark Conflict]
    G -->|No| I{Ambiguous?}
    I -->|Yes| J[LLM Conflict Check]
    I -->|No| K[No Conflict]
    
    J --> L[LLM Analysis:<br/>Do these points<br/>contradict each other?]
    L --> H
    
    H --> M[Resolve Conflicts]
    
    M --> N[Compare Priority]
    N --> O[Compare Verification Stats]
    O --> P[Deactivate Lower<br/>Priority Point]
    
    P --> Q[Update Point Status<br/>- active = false<br/>- replaced_by = ...]
    
    style D fill:#E6E6FA
    style E fill:#E6E6FA
    style J fill:#FFE4B5
    style M fill:#F0E68C
```

### 4. LLM Optimization Flow

```mermaid
graph TD
    A[All Traces Collected] --> B{Min Traces<br/>Threshold Met?}
    
    B -->|No| C[Skip LLM Optimization]
    B -->|Yes| D[Analyze Traces]
    
    D --> E[Calculate Metrics<br/>- Success rate<br/>- Latency<br/>- Attempts]
    
    E --> F[Extract Patterns<br/>- Common failures<br/>- Successful strategies<br/>- Tool usage patterns]
    
    F --> G[Compare with<br/>Ground Truth]
    
    G --> H[Build LLM Prompt<br/>with:<br/>- Trace summaries<br/>- Metrics<br/>- Current prompts<br/>- Reward spec]
    
    H --> I[LLM Call<br/>Generate Insights]
    
    I --> J[LLM Response<br/>JSON Insights]
    
    J --> K[Parse Insights<br/>- Category mapping<br/>- Priority assignment]
    
    K --> L[Add to GuidelineGenerator]
    
    L --> M[Convert to Points]
    
    style D fill:#E6E6FA
    style I fill:#FFE4B5
    style M fill:#DDA0DD
```

### 5. Prompt Rebuilding Process

```mermaid
graph TD
    A[All Points Processed] --> B[Get Active Points<br/>active=true<br/>source='original' or 'learned']
    
    B --> C[Group by Category]
    
    C --> D[Sort by Priority<br/>Higher priority first]
    
    D --> E[Build Prompt Structure]
    
    E --> F[1. Original System Prompt<br/>Base content unchanged]
    
    F --> G[2. Learned Insights Section<br/>All verified learned points]
    
    G --> H[Format Points:<br/>- Category headers<br/>- Bullet points<br/>- Priority ordering]
    
    H --> I[Final Prompt<br/>YAML Format]
    
    I --> J[Save to File]
    
    J --> K[Use in Next Round]
    
    style E fill:#DDA0DD
    style G fill:#90EE90
    style I fill:#FFE4B5
```

## Key Features

### Point Lifecycle

1. **Creation**: Points created from original prompts (parsing) or learned insights (LLM generation)
2. **Usage Tracking**: Points identified as "used" during execution via heuristic/LLM analysis
3. **Validation**: Points validated based on success/failure of executions where they were used
4. **Verification**: Points marked as "verified" after sufficient successful uses (≥3 verifications, ≥2 successes)
5. **Conflict Resolution**: Conflicting points resolved by deactivating lower-priority ones
6. **Rebuilding**: Prompts rebuilt from active, verified points

### Learning Mechanisms

1. **Heuristic-First Point Identification**: Fast pattern matching before LLM analysis
2. **LLM-Based Point Identification**: Deep analysis for ambiguous cases
3. **Batch Processing**: All traces processed together after round completion
4. **Incremental Conflict Detection**: Only check conflicts involving new points
5. **Caching**: Conflict detection results cached to reduce LLM calls
6. **Rate Limiting**: LLM calls rate-limited with exponential backoff retries

### Multi-Round Accumulation

- **Round 1**: Starts with original prompts only
- **Round 2+**: Loads accumulated points from previous rounds
- **Progressive Learning**: Insights accumulate across rounds
- **Preservation**: Verified points preserved across rounds
- **Refinement**: New insights refine and improve prompts

## Color Legend

- 🟢 **Green**: Start/End points
- 🟣 **Purple**: Data processing/transformation
- 🟡 **Yellow**: LLM operations
- 🔵 **Blue**: Heuristic/non-LLM operations
- 🟠 **Orange**: Conflict detection/resolution

