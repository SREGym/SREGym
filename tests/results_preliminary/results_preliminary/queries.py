import json
from pathlib import Path

import pandas as pd

all_results_csv = Path(__file__).parent / "stratus_12-29_09-34_resource_request_too_large_results.csv"
all_results_csv = pd.read_csv(all_results_csv)

# read row by row to find problem id
# glob get problem id jsonl file
# find steps with lily's old pandas script

pd.set_option("display.max_columns", None)


def load_jsonl_into_df(problem_id):
    rows = []
    jsonl_path = list(Path(__file__).parent.rglob(f"*{problem_id}*.jsonl"))[0]
    with open(jsonl_path, "r") as file:
        first_obj = json.loads(next(file))
        problem = first_obj.get("problem_id", "")
        for line in file:
            obj = json.loads(line)
            stage = obj.get("stage", "")
            num_steps = obj.get("num_steps", 0)
            messages = obj.get("messages", [])
            for dict in messages:
                rows.append(
                    {
                        "problem_id": problem,
                        "types": dict.get("type", ""),
                        "contents": dict.get("content", ""),
                        "tool_calls": dict.get("tool_calls", ""),
                        "stage": stage,
                        "num_steps": num_steps,
                    }
                )
        print(f"Loaded {jsonl_path} with problem_id: {problem}")

    df = pd.DataFrame(rows)
    return df


problem_ids = all_results_csv["problem_id"].unique()
problem_dfs = {}
for problem_id in problem_ids:
    problem_dfs[problem_id] = load_jsonl_into_df(problem_id)
    break
print(problem_dfs)
