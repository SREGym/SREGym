from sregym.conductor.oracles.llm_as_a_judge.judge import LLMJudge


def test_content_text_accepts_multimodal_blocks() -> None:
    assert LLMJudge._content_text([{"type": "text", "text": '{"judgment": "True"}'}]) == ('{"judgment": "True"}')


def test_content_text_accepts_string_content() -> None:
    assert LLMJudge._content_text("  answer  ") == "answer"
