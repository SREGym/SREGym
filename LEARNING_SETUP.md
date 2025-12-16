# Multi-Round Learning Setup Guide

This guide explains how to set up and run the multi-round learning script (`run_5_rounds_learning.py`) in SREGym.

## Prerequisites

1. **Copy Meta Agent from SREArena**
   The learning script requires the `meta_agent` directory from SREArena. Copy it to SREGym:
   
   ```bash
   cp -r /raid/xinbowu2/SREArena/meta_agent /home/xinbowu2/project/xinbowu2/SREGym/
   ```

2. **Set API Key**
   The script requires a Google/Gemini API key for LLM optimization:
   
   ```bash
   export GOOGLE_API_KEY='your-api-key-here'
   # OR
   export GEMINI_API_KEY='your-api-key-here'
   ```

3. **Ground Truth File (Optional)**
   If you have ground truth data, place it as `ground_truth_by_problem.json` in the SREGym root directory.

## Files Created

1. **`run_5_rounds_learning.py`** - Main learning script adapted for SREGym
2. **`mcp_tool_interceptor.py`** - MCP tool call interceptor for trace collection

## Key Adaptations from SREArena

- Changed imports from `srearena` to `sregym`
- Changed MCP server import from `srearena_mcp_server` to `sregym_mcp_server`
- Uses SREGym's `Conductor` and `conductor_api`
- Handles `StartProblemResult.SKIPPED_KHAOS_REQUIRED` for emulated clusters
- Uses SREGym's Stratus agent driver

## Usage

```bash
# Run with default settings (5 rounds)
python run_5_rounds_learning.py

# Customize number of rounds
python run_5_rounds_learning.py --num-rounds 3

# Customize delays
python run_5_rounds_learning.py --delay 60 --delay-between-rounds 600

# Customize reward weights
python run_5_rounds_learning.py --success-weight 2.0 --latency-weight -0.3 --attempts-weight -0.2

# Use different LLM model
python run_5_rounds_learning.py --model "gemini/gemini-2.0-flash-exp"
```

## Command Line Arguments

- `--delay`: Delay between problems in seconds (default: 30)
- `--delay-between-rounds`: Delay between rounds in seconds (default: 300)
- `--model`: LLM model to use (default: "gemini/gemini-2.5-flash")
- `--success-weight`: Weight for success rate optimization (default: 1.0)
- `--latency-weight`: Weight for latency optimization, negative (default: -0.5)
- `--attempts-weight`: Weight for attempts optimization, negative (default: -0.3)
- `--num-rounds`: Number of learning rounds to run (default: 5)

## Output Structure

Results are saved in `llm_learning_results/`:

```
llm_learning_results/
├── 5_rounds_<timestamp>/
│   ├── round_1_info.json
│   ├── round_2_info.json
│   ├── ...
│   └── summary.json
└── run_<timestamp>/
    ├── traces/          # Execution traces
    ├── prompts/         # Learned prompts
    ├── configs/         # Agent configs
    └── learning_results.json
```

## How It Works

1. **Round 1**: Starts with clean original prompts, executes all problems, collects traces
2. **Round 2+**: Loads prompts from previous round (with accumulated insights), executes problems, generates new insights
3. **After Each Round**: LLM analyzes traces and generates new insights to add to prompts
4. **Insight Accumulation**: Insights build across rounds, preserving verified insights

## Troubleshooting

- **Import Error for meta_agent**: Make sure you've copied the `meta_agent` directory from SREArena
- **API Key Error**: Set `GOOGLE_API_KEY` or `GEMINI_API_KEY` environment variable
- **Khaos Required**: Some problems require Khaos and will be skipped on emulated clusters
- **Port Conflicts**: Make sure ports 8000 (API) and 9954 (MCP) are available




