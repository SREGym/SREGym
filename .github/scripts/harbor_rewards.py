"""Check that every trial in a Harbor job finished with the expected reward.

Usage: python3 harbor_rewards.py <job-dir> <expected-reward>
"""

import json
import sys
from pathlib import Path


def main() -> int:
    job_dir, expected = Path(sys.argv[1]), float(sys.argv[2])
    # The job's own result.json sits at the top; each trial has its own directory.
    results = sorted(job_dir.glob("*/result.json"))
    if not results:
        print(f"No trial results under {job_dir}")
        return 1
    failed = False
    for path in results:
        trial = json.loads(path.read_text())
        rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
        error = trial.get("exception_info")
        reward = rewards.get("reward")
        print(
            f"{trial.get('trial_name', path.parent.name)}: reward={reward} exception={error and error.get('exception_type')}"
        )
        if error or reward is None or float(reward) != expected:
            if error:
                print(error.get("exception_message", ""))
            failed = True
    if failed:
        print(f"Expected reward {expected} for every trial")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
