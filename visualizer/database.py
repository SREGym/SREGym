import json
from pathlib import Path
import sqlite3
import pandas as pd
from tabulate import tabulate


database_path = "postgresql://lilygn@localhost:5432/postgres"
files = sorted(Path.cwd().rglob("*.jsonl"))
pd.set_option('display.max_columns', None)
def load_database():
    rows = []
    for path in files:
        with open(path, 'r') as file:
            first_obj = json.loads(next(file)) 
            problem = first_obj.get('problem_id', '')
            for line in file:
                obj = json.loads(line)
                stage = obj.get('stage', '')
                num_steps = obj.get('num_steps', 0)
                messages = obj.get('messages', [])
                for dict in messages:
                    rows.append( {
                        'problem_id': problem,
                        'types': dict.get('type', ''),
                        'contents': dict.get('content', ''),
                        'tool_calls': dict.get('tool_calls', ''),
                        'stage': stage,
                        'num_steps': num_steps
                    })
                print (f"Loaded {path} with problem_id: {problem}")

    df = pd.DataFrame(rows)
    return df

# def initialize_table():
#     create_table = """ CREATE TABLE IF NOT EXISTS results (
#     id INTEGER PRIMARY KEY,
#     problem_id TEXT NOT NULL,
#     types TEXT,
#     contents TEXT,
#     tool_calls TEXT
#     ); """

#     indexes = """ CREATE INDEX IF NOT EXISTS idx_problem_id ON results (problem_id); """
#     conn = sqlite3.connect("results.db")
#     cursor = conn.cursor()
#     cursor.execute(create_table)
#     cursor.execute(indexes)
#     conn.commit()
#     conn.close()
    

def results_to_df(results):
    csv_paths = sorted(Path.cwd().rglob("*results.csv"))
    if not csv_paths:
        print("No results.csv files found.")
        return pd.DataFrame()
    all_results = []
    for path in csv_paths:
        df = pd.read_csv(path)
        all_results.append(df)
    results_df = pd.concat(all_results, ignore_index=True)
    return results_df

def merge_results(messages_df, results_df):
    messages_df = messages_df.copy()
    messages_df["problem_id"] = messages_df["problem_id"].astype(str)
    results_df = results_df.copy()
    results_df["problem_id"] = results_df["problem_id"].astype(str)
    merged_df = pd.merge(messages_df, results_df, on="problem_id", how="inner", suffixes=('_msg', '_res'))
    return merged_df   


def avg_num_steps(df, stage_name):
    per_problem = (df[df['stage'] == stage_name]
                   .groupby('problem_id')['num_steps']
                   .max())
    return per_problem.mean()
def avg_steps_localization(df):
    return avg_num_steps(df, 'localization')
def avg_mitigation_steps(df):
    return avg_num_steps(df, 'mitigation_attempt_0')



 
if __name__ == "__main__":
    df = load_database()
    print(df.head())
    print("columns:", df.columns)
    print("number of rows:", len(df))
    print("number of unique problem_ids:", df['problem_id'].nunique())

    print("Sample data:")
    mask = df["tool_calls"].apply(lambda lst: isinstance(lst, list) and any(x not in (None, "", [], {}) for x in lst))

    df_with_tools = df[mask]
    print(df_with_tools.head(5))
    print("avg localization steps:", avg_steps_localization(df))
    print("avg mitigation steps:", avg_mitigation_steps(df))
    results = results_to_df(df)
    merged_df = merge_results(df, results)
    print("Merged DataFrame sample:")
    print(merged_df.head())
    per_problem_steps = (df.groupby('problem_id')['num_steps']
                         .max()
                         .reset_index())
    max_steps_problem = per_problem_steps.loc[per_problem_steps['num_steps'].idxmax()]
    print("Problem with most steps:")
    print(max_steps_problem)
    per_problem = (df.groupby('problem_id')['num_steps']
                   .max())
    most_steps_problem = per_problem.idxmax()
    most_steps_value = per_problem.max()
    print(f"Problem with most steps: {most_steps_problem} with {most_steps_value} steps")
    df.to_csv("all_messages.csv", index=False)
