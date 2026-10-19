"""Retry unacknowledged session writes before requests that depend on them.

The upstream planner schedules actions without reading HTTP responses. Keep
that schedule, but do not edit a message whose create request never succeeded.
Acknowledged writes are never recreated: losing those records must still fail.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

CREATORS = {"session_post": "root_id", "session_file_upload": "file_id"}
DEPENDENCIES = {
    "session_reply": "session_post",
    "session_thread": "session_post",
    "session_reaction": "session_post",
    "session_edit": "session_post",
    "session_delete": "session_post",
    "session_file_download": "session_file_upload",
}


@dataclass
class Creation:
    driver: object
    arguments: dict
    acknowledged: bool = False
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    retry_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionDependencies:
    def __init__(self, max_owned: int):
        # Match the planner's bounded history. An evicted write is never
        # recreated, so eviction cannot hide loss of acknowledged data.
        self.max_owned = max_owned
        self.sessions: dict[tuple, OrderedDict[str, Creation]] = {}

    def history(self, plan, creator: str) -> OrderedDict[str, Creation]:
        return self.sessions.setdefault((plan.session_id, creator), OrderedDict())

    async def request(self, driver, session, arguments):
        plan = arguments.get("plan")
        if plan is None:
            return await driver.request(session, **arguments)
        creator = driver.name if driver.name in CREATORS else DEPENDENCIES[driver.name]
        identifier = getattr(plan, CREATORS[creator])
        history = self.history(plan, creator)

        if driver.name == creator:
            creation = Creation(driver, dict(arguments))
            history[identifier] = creation
            while len(history) > self.max_owned:
                history.popitem(last=False)
            try:
                result = await driver.request(session, **arguments)
                creation.acknowledged = result.ok and result.correct is True
                return result
            finally:
                creation.completed.set()

        started = arguments["loop_time"]()
        creation = history.get(identifier)
        if creation is not None:
            await creation.completed.wait()
            async with creation.retry_lock:
                if not creation.acknowledged:
                    result = await creation.driver.request(session, **creation.arguments)
                    creation.acknowledged = result.ok and result.correct is True
                    if not creation.acknowledged:
                        result.latency_ms = (arguments["loop_time"]() - started) * 1000
                        return result
        result = await driver.request(session, **arguments)
        result.latency_ms = (arguments["loop_time"]() - started) * 1000
        return result


class SessionDriver:
    def __init__(self, driver, dependencies):
        self.driver = driver
        self.dependencies = dependencies
        self.name, self.op, self.target = driver.name, driver.op, driver.target

    async def request(self, session, **arguments):
        return await self.dependencies.request(self.driver, session, arguments)


def register(drivers: dict, max_owned: int) -> None:
    """Replace only session drivers in the engine's supported driver registry."""
    dependencies = SessionDependencies(max_owned)
    for name in CREATORS.keys() | DEPENDENCIES.keys():
        drivers[name] = SessionDriver(drivers[name], dependencies)
