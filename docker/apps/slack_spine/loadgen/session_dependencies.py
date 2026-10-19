"""Retry unacknowledged session writes before requests that depend on them.

The upstream planner schedules actions without reading HTTP responses. Keep
that schedule, but do not edit a message whose create request never succeeded.
Acknowledged writes are never recreated: losing those records must still fail.
"""

from __future__ import annotations

import asyncio
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
    def __init__(self):
        self.creations: dict[tuple, Creation] = {}

    def _discard_acknowledged(self, key: tuple, creation: Creation) -> None:
        if creation.acknowledged and self.creations.get(key) is creation:
            del self.creations[key]

    @staticmethod
    def _acknowledged(driver, result) -> bool:
        # File upload reports `ok` for the metadata POST and `correct` for a
        # separate completion POST. Downloads query that metadata, so retrying
        # an already acknowledged create must not depend on completion again.
        return result.ok and (driver.name == "session_file_upload" or result.correct is True)

    async def request(self, driver, session, arguments):
        plan = arguments.get("plan")
        if plan is None:
            return await driver.request(session, **arguments)
        creator = driver.name if driver.name in CREATORS else DEPENDENCIES[driver.name]
        identifier = getattr(plan, CREATORS[creator])
        key = (plan.session_id, creator, identifier)

        if driver.name == creator:
            creation = Creation(driver, dict(arguments))
            self.creations[key] = creation
            try:
                result = await driver.request(session, **arguments)
                creation.acknowledged = self._acknowledged(driver, result)
                return result
            finally:
                creation.completed.set()
                self._discard_acknowledged(key, creation)

        started = arguments["loop_time"]()
        creation = self.creations.get(key)
        if creation is not None:
            await creation.completed.wait()
            async with creation.retry_lock:
                if not creation.acknowledged:
                    result = await creation.driver.request(session, **creation.arguments)
                    creation.acknowledged = self._acknowledged(creation.driver, result)
                    self._discard_acknowledged(key, creation)
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


def register(drivers: dict) -> None:
    """Replace only session drivers in the engine's supported driver registry."""
    dependencies = SessionDependencies()
    for name in CREATORS.keys() | DEPENDENCIES.keys():
        drivers[name] = SessionDriver(drivers[name], dependencies)
