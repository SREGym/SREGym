"""Stage-aware submission transport for the opt-in Jev experiment."""

import asyncio
import os

import httpx


class SubmissionClient:
    def __init__(self, *, transport=None):
        host = os.getenv("API_HOSTNAME", "localhost")
        port = os.getenv("API_PORT", "8000")
        self.base_url = f"http://{host}:{port}"
        self.transport = transport
        self.accepted: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    async def submit(self, stage: str, solution: str) -> dict:
        # Serialize submissions, but not investigation or review calls. The
        # Conductor remains the authority for acceptance and stage transitions.
        async with self.lock:
            if stage in self.accepted:
                return self.accepted[stage]
            async with httpx.AsyncClient(transport=self.transport, timeout=10, follow_redirects=False) as client:
                try:
                    response = await client.get(f"{self.base_url}/status")
                    response.raise_for_status()
                    current = response.json().get("stage")
                except (httpx.HTTPError, ValueError, AttributeError):
                    return {"status": "not_submitted", "reason": "Cannot read the submission stage. Retry later."}
                if current != stage:
                    return {
                        "status": "not_submitted",
                        "reason": f"Requested stage {stage!r} is not current; current stage is {current!r}. Wait if diagnosis grading is still running.",
                    }
                try:
                    response = await client.post(f"{self.base_url}/submit", json={"stage": stage, "solution": solution})
                except httpx.RequestError:
                    return {
                        "status": "submission_unknown",
                        "reason": "The submission response was lost. Check the Conductor stage before retrying; do not assume success.",
                    }
                if response.status_code != 200:
                    return {"status": "not_submitted", "http_status": response.status_code}
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if not isinstance(body, dict) or body.get("status") != "200" or body.get("stage") != stage:
                    return {"status": "submission_unknown", "reason": "Conductor did not confirm stage acceptance."}
                result = {"status": "accepted", "stage": stage}
                self.accepted[stage] = result
                return result
