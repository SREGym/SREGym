"""LLM-as-a-Judge oracle module."""

from sregym.conductor.oracles.llm_as_a_judge.fault_spec_extractor import (
    FaultSpec,
    extract_fault_spec,
    extract_fault_spec_dict,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle

__all__ = [
    "LLMAsAJudgeOracle",
    "FaultSpec",
    "extract_fault_spec",
    "extract_fault_spec_dict",
]
