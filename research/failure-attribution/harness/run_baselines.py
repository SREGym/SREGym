"""Run the paper's three baselines (unchanged prompts) on any OpenAI-compatible endpoint.

Automated_FA/inference.py only builds an AzureOpenAI client. Its methods just call
`client.chat.completions.create`, so an `openai.OpenAI` client pointed at OpenRouter (or
vLLM, Ollama, ...) works as is. That way the baselines use the same model as TracePress.

    OPENROUTER_API_KEY=... python run_baselines.py --afa path/to/Agents_Failure_Attribution \
        --subset hand --method all_at_once --model openai/gpt-6-luna
"""

import argparse
import contextlib
import os
import sys
from pathlib import Path

from openai import OpenAI

SUBSETS = {"hand": ("Hand-Crafted", True), "algo": ("Algorithm-Generated", False)}

parser = argparse.ArgumentParser()
parser.add_argument("--afa", required=True, help="checkout of ag2ai/Agents_Failure_Attribution")
parser.add_argument("--subset", choices=SUBSETS, required=True)
parser.add_argument("--method", choices=("all_at_once", "step_by_step", "binary_search"), required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
parser.add_argument("--max-tokens", type=int, default=1024)
args = parser.parse_args()

sys.path.insert(0, str(Path(args.afa) / "Automated_FA"))
from Lib import utils  # noqa: E402

folder, is_handcrafted = SUBSETS[args.subset]
client = OpenAI(base_url=args.base_url, api_key=os.environ[args.api_key_env])
out = Path(f"outputs/{args.method}_{args.model.replace('/', '_')}_{args.subset}.txt")
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", encoding="utf-8") as f, contextlib.redirect_stdout(f):
    getattr(utils, args.method)(
        client=client,
        directory_path=str(Path(args.afa) / "Who&When" / folder),
        is_handcrafted=is_handcrafted,
        model=args.model,
        max_tokens=args.max_tokens,
    )
print(f"wrote {out}")
