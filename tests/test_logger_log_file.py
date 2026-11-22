import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

import logger


@pytest.fixture
def same_minute(monkeypatch):
    monkeypatch.setattr(logger, "get_current_datetime_formatted", lambda: "1106_1358")


def test_runs_started_in_the_same_minute_get_separate_log_files(tmp_path, same_minute):
    names = [logger.claim_log_file(tmp_path).name for _ in range(3)]

    assert names == ["sregym_1106_1358.log", "sregym_1106_1358_2.log", "sregym_1106_1358_3.log"]


def test_concurrent_claims_never_share_a_log_file(tmp_path, same_minute):
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: logger.claim_log_file(tmp_path), range(16)))

    assert len(set(claimed)) == 16
    assert all(path.is_file() for path in claimed)


def test_claim_creates_the_log_directory(tmp_path, same_minute):
    path = logger.claim_log_file(tmp_path / "logs" / "nested")

    assert path == tmp_path / "logs" / "nested" / "sregym_1106_1358.log"
    assert path.is_file()


def test_init_logger_does_not_append_to_an_earlier_runs_log(tmp_path, monkeypatch, same_minute):
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    # These replace the real root logger's handlers and change library log levels. Stub them so
    # the test only touches the "all" logger, which it restores below.
    for helper in (
        "unify_third_party_loggers",
        "silent_litellm_loggers",
        "silent_httpx_loggers",
        "silent_FastMCP_loggers",
    ):
        monkeypatch.setattr(logger, helper, lambda: None)
    earlier = tmp_path / "sregym_1106_1358.log"
    earlier.write_text("earlier run\n")
    root_logger = logging.getLogger("all")
    saved = root_logger.handlers[:]
    root_logger.handlers = []
    try:
        logger.init_logger()
        logging.getLogger("all.test").info("this run")
        new_handlers = root_logger.handlers[:]
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers = saved

    file_handler = next(h for h in new_handlers if isinstance(h, logging.FileHandler))
    assert file_handler.baseFilename == str(tmp_path / "sregym_1106_1358_2.log")
    assert earlier.read_text() == "earlier run\n"
    assert "this run" in (tmp_path / "sregym_1106_1358_2.log").read_text()
