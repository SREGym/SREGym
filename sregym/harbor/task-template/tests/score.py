"""Turn the SREGym mitigation verdict into a Harbor reward.

The grade is produced inside the sregym sidecar by the problem's mitigation
oracle and collected as an artifact. A missing or unusable grade is an
infrastructure failure, so no reward is written and Harbor records an error
instead of scoring the agent zero.
"""

import json
import sys
from pathlib import Path

GRADE = Path("{{grade_path}}")
REWARD = Path("/logs/verifier/reward.json")


def main() -> int:
    try:
        grade = json.loads(GRADE.read_text())
    except FileNotFoundError:
        print(f"No grade was collected from the SREGym sidecar ({GRADE}).", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Unreadable SREGym grade: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(grade, indent=2))
    if "mitigation" not in grade:
        print(f"SREGym backend could not grade this trial: {grade.get('error')}", file=sys.stderr)
        return 1

    reward = 1.0 if grade.get("success") is True else 0.0
    REWARD.parent.mkdir(parents=True, exist_ok=True)
    REWARD.write_text(json.dumps({"reward": reward}) + "\n")
    print(f"Mitigation {'succeeded' if reward else 'failed'}: reward={reward}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
