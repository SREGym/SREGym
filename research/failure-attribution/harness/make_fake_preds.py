"""Oracle and random prediction files in evaluate.py's format, to sanity-check the scorer.

python -I make_fake_preds.py <path to Who&When> hand|algo
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from whowhen_tp import load, normalize  # noqa: E402


def oracle(trace: dict, rng: random.Random) -> tuple[str, int]:
    return trace["mistake_agent"], int(trace["mistake_step"])


def uniform_step(trace: dict, rng: random.Random) -> tuple[str, int]:
    """A uniformly random step, attributed to whoever spoke at it."""
    step = rng.randrange(len(trace["history"]))
    return normalize(trace["speakers"][step]), step


def main(data: Path, subset: str) -> None:
    traces = load(data, subset)
    Path("outputs").mkdir(exist_ok=True)
    for name, pick in {"oracle": oracle, "random": uniform_step}.items():
        rng = random.Random(0)
        blocks = []
        for tid, trace in traces.items():
            agent, step = pick(trace, rng)
            blocks.append(f"Prediction for {tid}:\nAgent Name: {agent}\nStep Number: {step}\n")
        Path(f"outputs/{name}_{subset}.txt").write_text("\n".join(blocks))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
