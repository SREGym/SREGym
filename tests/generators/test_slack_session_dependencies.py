import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PATH = Path(__file__).resolve().parents[2] / "docker/apps/slack_spine/loadgen/session_dependencies.py"
SPEC = importlib.util.spec_from_file_location("slack_session_dependencies", PATH)
dependencies = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dependencies
SPEC.loader.exec_module(dependencies)


class Result:
    # Match the upstream protocol: mutable attributes, not a dataclass.
    __slots__ = ("status", "ok", "correct", "latency_ms")

    def __init__(self, status=200, ok=True, correct=True, latency_ms=1.0):
        self.status, self.ok, self.correct, self.latency_ms = status, ok, correct, latency_ms


def driver(name, **kwargs):
    return SimpleNamespace(name=name, op="POST", target="/messages", request=AsyncMock(**kwargs))


def arguments(identifier="one"):
    plan = SimpleNamespace(session_id=1, root_id=identifier, file_id=identifier)
    return dict(plan=plan, seq=1, x="request", loop_time=time.monotonic)


@pytest.mark.parametrize("operation", ["session_reaction", "session_edit", "session_delete", "session_reply"])
def test_retry_only_unacknowledged_create_before_dependent_request(operation):
    async def scenario():
        state = dependencies.SessionDependencies()
        create = driver("session_post", side_effect=[Result(status=503, ok=False, correct=False), Result()])
        dependent = driver(operation, return_value=Result())
        kwargs = arguments()
        assert (await state.request(create, None, kwargs)).ok is False
        assert (await state.request(dependent, None, kwargs)).ok is True
        assert create.request.await_count == 2
        assert create.request.call_args_list[0] == create.request.call_args_list[1]
        dependent.request.assert_awaited_once()

    asyncio.run(scenario())


def test_loss_of_acknowledged_data_is_not_recreated_or_hidden():
    async def scenario():
        state = dependencies.SessionDependencies()
        create = driver("session_post", return_value=Result())
        dependent = driver("session_edit", return_value=Result(status=404, ok=False, correct=False))
        await state.request(create, None, arguments())
        result = await state.request(dependent, None, arguments())
        assert result.status == 404 and not result.ok
        create.request.assert_awaited_once()

    asyncio.run(scenario())


def test_active_outage_is_still_a_failure():
    async def scenario():
        state = dependencies.SessionDependencies()
        create = driver("session_post", return_value=Result(status=503, ok=False, correct=False))
        dependent = driver("session_edit", return_value=Result())
        await state.request(create, None, arguments())
        result = await state.request(dependent, None, arguments())
        assert result.status == 503 and not result.ok
        dependent.request.assert_not_awaited()

    asyncio.run(scenario())


def test_pending_create_is_awaited_and_concurrent_dependents_retry_once():
    async def scenario():
        state = dependencies.SessionDependencies()
        started, finish = asyncio.Event(), asyncio.Event()

        async def first(*args, **kwargs):
            if started.is_set():
                return Result()
            started.set()
            await finish.wait()
            return Result(status=503, ok=False, correct=False)

        create = driver("session_post", side_effect=first)
        dependent = driver("session_reaction", return_value=Result())
        post = asyncio.create_task(state.request(create, None, arguments()))
        await started.wait()
        tasks = [asyncio.create_task(state.request(dependent, None, arguments())) for _ in range(3)]
        await asyncio.sleep(0)
        dependent.request.assert_not_awaited()
        finish.set()
        await post
        assert all(result.ok for result in await asyncio.gather(*tasks))
        assert create.request.await_count == 2

    asyncio.run(scenario())


def test_long_outage_does_not_evict_a_failed_create_and_successes_do_not_accumulate():
    async def scenario():
        state = dependencies.SessionDependencies()
        create = driver("session_post", side_effect=[Result(status=503, ok=False), *[Result() for _ in range(129)]])
        await state.request(create, None, arguments("old"))
        for number in range(128):
            await state.request(create, None, arguments(f"new-{number}"))
        assert len(state.creations) == 1
        dependent = driver("session_delete", return_value=Result())
        assert (await state.request(dependent, None, arguments("old"))).ok
        assert create.request.await_count == 130
        assert not state.creations

    asyncio.run(scenario())


def test_file_dependency_and_non_session_requests():
    async def scenario():
        state = dependencies.SessionDependencies()
        upload = driver("session_file_upload", side_effect=[Result(status=503, ok=False), Result()])
        download = driver("session_file_download", return_value=Result())
        await state.request(upload, None, arguments())
        assert (await state.request(download, None, arguments())).ok
        assert upload.request.await_count == 2
        original = driver("session_post", return_value=Result())
        assert (await state.request(original, None, {"plan": None})).ok
        original.request.assert_awaited_once_with(None, plan=None)

    asyncio.run(scenario())
