"""Snapshot trusted oracle state without transporting live clients or credentials.

Pickle is used only on the runner's own objects, over its private Docker pipes.
Never load a snapshot supplied by an evaluated agent.
"""

import importlib
import io
import os
import pickle
import subprocess
import threading
from pathlib import Path, PurePath

import cloudpickle

IMAGE_ROOT = Path("/opt/sregym")
_LOCK_TYPE = type(threading.Lock())
_RLOCK_TYPE = type(threading.RLock())


def _new_instance(cls):
    return object.__new__(cls)


class OraclePickler(cloudpickle.CloudPickler):
    def __init__(self, file, repo_root: Path):
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.repo_root = repo_root.resolve()
        self.workloads = []

    def persistent_id(self, obj):
        from sregym.generators.workload.hotel_search import HotelSearchWorkload
        from sregym.service.codehub_verification_journal import CodeHubVerificationJournal
        from sregym.service.kubectl import KubeCtl

        if isinstance(obj, ModelHandle):
            return ("model", obj.index)

        if isinstance(obj, KubeCtl):
            return ("kubectl", id(obj))
        cls = type(obj)
        if cls.__module__.startswith("kubernetes.client.api.") and cls.__name__.endswith("Api"):
            return ("api", id(obj), cls.__module__, cls.__name__)
        if isinstance(obj, (HotelSearchWorkload, CodeHubVerificationJournal)):
            for index, existing in enumerate(self.workloads):
                if existing is obj:
                    return ("workload", index)
            self.workloads.append(obj)
            return ("workload", len(self.workloads) - 1)
        if isinstance(obj, (threading.Thread, subprocess.Popen)):
            raise TypeError("A live thread/process requires an explicit verifier state adapter")
        if isinstance(obj, (_LOCK_TYPE, _RLOCK_TYPE)):
            return ("lock", id(obj), isinstance(obj, _RLOCK_TYPE))
        if isinstance(obj, threading.Event):
            return ("event", id(obj), obj.is_set())
        return None

    def reducer_override(self, obj):
        from sregym.conductor.problems.base import Problem
        from sregym.generators.workload.stream import StreamWorkloadManager

        if isinstance(obj, PurePath):
            try:
                relative = obj.relative_to(self.repo_root)
            except ValueError:
                return (Path, (str(obj),))
            return (Path, (str(IMAGE_ROOT.joinpath(*relative.parts)),))
        if isinstance(obj, Problem):
            state = dict(vars(obj))
            # Diagnosis models are not part of mitigation state and may hold
            # provider credentials and nonserializable transports.
            state["diagnosis_oracle"] = None
            for name in obj.verifier_excluded_fields:
                state[name] = None
            return (_new_instance, (type(obj),), state)
        if isinstance(obj, StreamWorkloadManager):
            state = dict(vars(obj))
            # This historically lives on the class, outside __dict__.
            state["log_history"] = list(obj.log_history)
            return (_new_instance, (type(obj),), state)
        from sregym.conductor.oracles.llm_as_a_judge.judge import DiagnosisJudge, LLMJudge

        if isinstance(obj, (DiagnosisJudge, LLMJudge)):
            backend = obj.backend
            if backend is None:
                raise ValueError("Diagnosis judge backend is not initialized")
            self.workloads.append(("model", backend))
            state = dict(vars(obj))
            # Scoring and prompts stay in the verifier. The owner performs
            # model IO through the private pipe without forwarding credentials.
            state.update(_backend=ModelHandle(len(self.workloads) - 1), api_key=None)
            state["model_name"] = getattr(backend, "model_name", None) or obj.model_name
            return (_new_instance, (type(obj),), state)
        return super().reducer_override(obj)


class OracleUnpickler(pickle.Unpickler):
    def __init__(self, file, workload_factory=None):
        super().__init__(file)
        self.cache = {}
        self.workload_factory = workload_factory

    def persistent_load(self, identifier):
        if identifier in self.cache:
            return self.cache[identifier]
        kind = identifier[0]
        if kind == "kubectl":
            from sregym.service.kubectl import KubeCtl

            value = KubeCtl()
        elif kind == "api":
            value = getattr(importlib.import_module(identifier[2]), identifier[3])()
        elif kind == "lock":
            value = threading.RLock() if identifier[2] else threading.Lock()
        elif kind == "event":
            value = threading.Event()
            if identifier[2]:
                value.set()
        elif kind in {"workload", "model"} and self.workload_factory is not None:
            value = self.workload_factory(identifier[1])
        else:
            raise pickle.UnpicklingError(f"Unsupported verifier resource: {kind}")
        self.cache[identifier] = value
        return value


def snapshot_oracle(oracle, repo_root: Path) -> tuple[bytes, list]:
    stream = io.BytesIO()
    pickler = OraclePickler(stream, repo_root)
    pickler.dump(oracle)
    return stream.getvalue(), pickler.workloads


def restore_oracle(payload: bytes, workload_factory=None):
    return OracleUnpickler(io.BytesIO(payload), workload_factory).load()


class ModelHandle:
    def __init__(self, index):
        self.index = index


def save_oracle_snapshot(oracle, path: Path) -> None:
    """Save a baseline-bearing snapshot for the standalone CLI (trusted input)."""
    payload, workloads = snapshot_oracle(oracle, Path(__file__).resolve().parents[2])
    if workloads:
        raise ValueError("Live host workloads require the conductor's private verifier channel")
    # Exclusive creation avoids replacing another attempt's baseline.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
