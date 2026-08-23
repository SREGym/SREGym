import asyncio
import json
import os

import pandas as pd

from clients.stratus.stratus_agent.driver import driver
from llm_backend.usage_log import USAGE_LOG_PATH_ENV


def test_completed_run_adds_summary_usage_to_csv(monkeypatch, tmp_path):
    problem_id = "test_problem"
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(driver, "resolve_problem_id", lambda: problem_id)

    async def fake_diagnosis_run():
        usage_record = {
            "usage_type": "summary",
            "input_tokens": 11,
            "output_tokens": 4,
            "total_tokens": 15,
            "duration_seconds": 0.25,
        }
        usage_log = os.environ[USAGE_LOG_PATH_ENV]
        with open(usage_log, "w", encoding="utf-8") as log_file:
            log_file.write(json.dumps(usage_record) + "\n")
        stats = {
            "input_tokens": 5,
            "output_tokens": 1,
            "total_tokens": 6,
            "time": "0.1",
            "steps": 1,
            "num_retry_attempts": "N/A",
            "rollback_stack": "N/A",
            "oracle_results": "N/A",
        }
        return stats, object(), []

    async def fake_stage_switch(**_kwargs):
        return "done"

    monkeypatch.setattr(driver, "diagnosis_with_localization_task_main", fake_diagnosis_run)
    monkeypatch.setattr(driver, "generate_run_summary", lambda *_args: "summary")
    monkeypatch.setattr(driver, "wait_for_stage_switch", fake_stage_switch)
    monkeypatch.setattr(driver, "save_combined_trajectory", lambda *_args, **_kwargs: None)

    asyncio.run(driver.main())

    output = pd.read_csv(tmp_path / f"{problem_id}_stratus_output.csv")
    assert output["agent_name"].tolist() == ["diagnosis_agent", "run_summary"]
    assert output["total_tokens"].tolist() == [6, 15]
