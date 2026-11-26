"""Shared Bedrock detection for the Claude Code and Codex clients and the host-side egress rules."""

from collections.abc import Mapping


def is_bedrock_model(model: str) -> bool:
    """bedrock/<inference-profile> runs the agent on AWS Bedrock with the forwarded AWS credentials."""
    return model.casefold().startswith("bedrock/")


def bedrock_region(environment: Mapping[str, str]) -> str:
    """The region the Claude Code and Codex CLIs call: AWS_REGION, then AWS_DEFAULT_REGION."""
    return (environment.get("AWS_REGION") or environment.get("AWS_DEFAULT_REGION") or "").strip()
