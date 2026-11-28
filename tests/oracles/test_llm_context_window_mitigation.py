from types import SimpleNamespace

import pytest
import yaml

import sregym.conductor.oracles.llm_context_window_mitigation as oracle_module
from sregym.conductor.oracles.llm_context_window_mitigation import LlmContextWindowMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.llm_context_window_astronomy_shop import MANIFEST_DIR, LlmContextWindowAstronomyShop

MODEL = LlmContextWindowAstronomyShop.MODEL_FILE
ANSWER = (200, '"Reviewers say it is an easy, portable entry-level telescope with clear views of the moon."')
REJECTED = (500, '{"message":"Internal Server Error"}')


def _oracle(monkeypatch, *, props=None, answers=None, asked=None):
    monkeypatch.setattr(MitigationOracle, "_evaluate_current_state", lambda self: {"success": True})
    monkeypatch.setattr(oracle_module.time, "sleep", lambda s: None)
    answers = answers or {}
    asked = asked if asked is not None else []

    def ask(pid):
        asked.append(pid)
        answer = answers.get(pid, ANSWER)
        return answer.pop(0) if isinstance(answer, list) else answer

    problem = SimpleNamespace(
        MODEL_FILE=MODEL,
        PROBE_PRODUCT_IDS=("OLJCESPC7Z", "66VCHSJNUP"),
        llm_server_props=lambda: {"model_path": f"/models/{MODEL}"} if props is None else props,
        ask_ai_assistant=ask,
        assistant_answered=LlmContextWindowAstronomyShop.assistant_answered,
    )
    return LlmContextWindowMitigationOracle(problem)


def test_passes_when_the_real_model_answers(monkeypatch):
    assert _oracle(monkeypatch)._evaluate_current_state() == {"success": True}


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"props": {}}, "llm_replaced"),
        ({"props": {"model_path": "/models/some-other-model.gguf"}}, "llm_replaced"),
        ({"answers": {"OLJCESPC7Z": REJECTED}}, "ai_assistant_failing"),
        ({"answers": {"66VCHSJNUP": REJECTED}}, "ai_assistant_failing"),
        (
            {"answers": {"OLJCESPC7Z": (200, '"The system is unable to process your response."')}},
            "ai_assistant_failing",
        ),
        ({"answers": {"OLJCESPC7Z": (None, "error: unable to upgrade connection")}}, "ai_assistant_failing"),
    ],
)
def test_fails_when_the_assistant_is_not_really_fixed(monkeypatch, kwargs, reason):
    result = _oracle(monkeypatch, **kwargs)._evaluate_current_state()
    assert result["success"] is False
    assert result["reason"] == reason


def test_retries_a_one_off_failure(monkeypatch):
    asked = []
    oracle = _oracle(monkeypatch, answers={"OLJCESPC7Z": [REJECTED, ANSWER]}, asked=asked)
    assert oracle._evaluate_current_state() == {"success": True}
    assert asked == ["OLJCESPC7Z", "OLJCESPC7Z", "66VCHSJNUP"]


def test_manifest_runs_llama_cpp_with_two_slots():
    with open(f"{MANIFEST_DIR}/llm_server.yaml") as f:
        docs = {d["kind"]: d for d in yaml.safe_load_all(f)}
    config = docs["ConfigMap"]["data"]
    assert config["LLAMA_ARG_N_PARALLEL"] == "2"
    assert int(config["LLAMA_ARG_CTX_SIZE"]) // LlmContextWindowAstronomyShop.FAULT_PARALLEL < 320
    assert config["LLAMA_ARG_MODEL"].endswith(MODEL)
    container = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [{"configMapRef": {"name": LlmContextWindowAstronomyShop.CONFIGMAP}}]
    assert docs["Service"]["metadata"]["name"] == "llm"
