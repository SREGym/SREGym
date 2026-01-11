from pathlib import Path

import pandas as pd

all_results_csv = Path(__file__).parent / "stratus_12-29_09-34_resource_request_too_large_results.csv"
all_results_csv = pd.read_csv(all_results_csv)

# read row by row to find problem id
# glob get problem id jsonl file
# find steps with lily's old pandas script
