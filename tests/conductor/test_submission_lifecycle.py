import asyncio
import concurrent.futures
import logging
import sys
import threading
import time
from types import MethodType, SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from sregym.conductor import conductor as conductor_module
from sregym.conductor import conductor_api
from sregym.conductor.conductor import (
    Conductor,
    EvaluationInProgress,
    SubmissionAttemptClosed,
    SubmissionAttemptMismatch,
)


def _conductor(diagnosis_evaluation=None, mitigation_evaluation=None) -> Conductor:
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("test.submission_lifecycle")
    conductor.config = SimpleNamespace(enable_noise=False)
    conductor.problem = None
    conductor._baseline_captured = False
    conductor.execution_start_time = time.time()
    conductor.results = {}
    conductor.stage_sequence = [
        {
            "name": "diagnosis",
            "evaluation": diagnosis_evaluation or (lambda _solution: None),
        },
        {
            "name": "mitigation",
            "evaluation": mitigation_evaluation or (lambda _solution: None),
        },
    ]
    conductor.current_stage_index = 0
    conductor.submission_stage = "diagnosis"
    conductor.waiting_for_agent = True
    conductor._evaluating = False
    conductor._submit_future = None
    conductor._submission_lock = threading.RLock()
    conductor._pending_submission_stages = {}
    conductor._submission_generation = 1
    conductor._aborted_submission_generations = set()
    conductor._accepting_submissions = True
    conductor._attempt_closed = False

    def advance(self, start_index=0):
        self.waiting_for_agent = False
        self.current_stage_index = start_index
        if start_index < len(self.stage_sequence):
            self.submission_stage = self.stage_sequence[start_index]["name"]
            self.waiting_for_agent = True
            self._accepting_submissions = not self._attempt_closed
        else:
            self.submission_stage = "done"
            self._accepting_submissions = False
            self._attempt_closed = True

    conductor._advance_to_next_stage = MethodType(advance, conductor)
    return conductor


def _wait_for_current_evaluation(conductor: Conductor) -> None:
    future = conductor._submit_future
    if future is not None:
        future.result(timeout=2)


def test_conductor_rejects_duplicate_submission_during_evaluation():
    gate = threading.Event()
    conductor = _conductor(diagnosis_evaluation=lambda _solution: gate.wait(2))

    async def run():
        response = await conductor.submit("diagnosis", expected_stage="diagnosis")
        assert response == {
            "status": "accepted",
            "message": "Submission received",
            "stage": "diagnosis",
        }
        with pytest.raises(EvaluationInProgress):
            await conductor.submit("duplicate", expected_stage="diagnosis")

    try:
        asyncio.run(run())
    finally:
        gate.set()
        _wait_for_current_evaluation(conductor)


def test_legacy_early_mitigation_waits_and_is_accepted_for_mitigation(monkeypatch):
    diagnosis_gate = threading.Event()
    mitigation_evaluated = threading.Event()

    def evaluate_diagnosis(solution):
        diagnosis_gate.wait(2)
        conductor.results["Diagnosis"] = {"success": True, "submission": solution}

    def evaluate_mitigation(_solution):
        conductor.results["Mitigation"] = {"success": True}
        mitigation_evaluated.set()

    conductor = _conductor(evaluate_diagnosis, evaluate_mitigation)
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        first = await conductor_api.submit_solution(
            conductor_api.SubmitRequest(stage="diagnosis", solution="diagnosis")
        )
        assert first["stage"] == "diagnosis"

        # No explicit stage emulates existing clients. The empty solution still
        # identifies this request as mitigation, so it cannot become a duplicate diagnosis.
        mitigation_request = asyncio.create_task(
            conductor_api.submit_solution(conductor_api.SubmitRequest(solution=""))
        )
        await asyncio.sleep(0.02)
        assert not mitigation_request.done()

        diagnosis_gate.set()
        second = await asyncio.wait_for(mitigation_request, timeout=2)
        assert second == {
            "status": "200",
            "message": "Submission received",
            "stage": "mitigation",
        }

    asyncio.run(run())
    _wait_for_current_evaluation(conductor)
    assert mitigation_evaluated.is_set()
    assert conductor.results["Diagnosis"]["submission"] == "diagnosis"
    assert conductor.results["Mitigation"]["success"] is True


def test_registered_early_mitigation_survives_atomic_agent_exit_close(monkeypatch):
    diagnosis_gate = threading.Event()

    def evaluate_diagnosis(solution):
        diagnosis_gate.wait(2)
        conductor.results["Diagnosis"] = {"success": True, "submission": solution}

    def evaluate_mitigation(_solution):
        conductor.results["Mitigation"] = {"success": True}

    conductor = _conductor(evaluate_diagnosis, evaluate_mitigation)
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        await conductor_api.submit_solution(conductor_api.SubmitRequest(stage="diagnosis", solution="diagnosis"))
        mitigation_request = asyncio.create_task(
            conductor_api.submit_solution(conductor_api.SubmitRequest(stage="mitigation", solution=""))
        )
        await asyncio.sleep(0.02)
        assert conductor.close_submissions() is True

        diagnosis_gate.set()
        mitigation = await asyncio.wait_for(mitigation_request, timeout=2)
        assert mitigation["stage"] == "mitigation"
        await conductor.wait_for_submission_work(timeout=2)

    asyncio.run(run())
    assert conductor.results["Diagnosis"]["success"] is True
    assert conductor.results["Mitigation"]["success"] is True
    assert conductor.submission_stage == "done"


def test_close_rejects_requests_that_arrive_after_agent_exit(monkeypatch):
    conductor = _conductor()
    monkeypatch.setattr(conductor_api, "_conductor", conductor)
    assert conductor.close_submissions() is False
    with pytest.raises(SubmissionAttemptClosed):
        conductor.register_pending_submission("diagnosis")

    async def run():
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/submit",
                json={"stage": "diagnosis", "solution": "late diagnosis"},
            )
            assert response.status_code == 409

    asyncio.run(run())
    assert conductor._submit_future is None


def test_closed_incomplete_attempt_does_not_claim_all_stages_were_graded(monkeypatch):
    conductor = _conductor()
    conductor.record_incomplete_attempt("agent_exited_before_all_stages_completed")
    conductor.submission_stage = "done"
    conductor.close_submissions()
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get("/status")
            late = await client.post("/submit", json={"stage": "mitigation", "solution": ""})
            assert status.json() == {"stage": "done"}
            assert late.status_code == 409
            assert "no longer accepts" in late.json()["detail"]

    asyncio.run(run())


def test_old_attempt_generation_cannot_submit_to_new_attempt():
    conductor = _conductor()
    old_generation = conductor.register_pending_submission("diagnosis")
    conductor.close_submissions()
    with conductor._submission_lock:
        conductor._submission_generation += 1
        conductor._attempt_closed = False
        conductor._accepting_submissions = True

    async def run():
        with pytest.raises(SubmissionAttemptMismatch):
            await conductor.submit(
                "old attempt",
                expected_stage="diagnosis",
                expected_generation=old_generation,
            )

    try:
        asyncio.run(run())
    finally:
        conductor.unregister_pending_submission("diagnosis", old_generation)


def test_duplicate_diagnosis_never_becomes_mitigation(monkeypatch):
    diagnosis_gate = threading.Event()
    conductor = _conductor(diagnosis_evaluation=lambda _solution: diagnosis_gate.wait(2))
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        await conductor_api.submit_solution(conductor_api.SubmitRequest(stage="diagnosis", solution="diagnosis"))
        with pytest.raises(HTTPException) as exc_info:
            await conductor_api.submit_solution(conductor_api.SubmitRequest(solution="duplicate diagnosis"))
        assert exc_info.value.status_code == 409

    try:
        asyncio.run(run())
    finally:
        diagnosis_gate.set()
        _wait_for_current_evaluation(conductor)

    assert conductor.submission_stage == "mitigation"
    assert "Mitigation" not in conductor.results


def test_mcp_reports_the_stage_that_accepted_the_submission(monkeypatch):
    conductor = _conductor()
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    response = asyncio.run(conductor_api.submit_via_conductor.fn("diagnosis", stage="diagnosis"))

    assert response == {
        "status": "200",
        "text": "Submission received",
        "stage": "diagnosis",
    }
    _wait_for_current_evaluation(conductor)


def test_legacy_nonempty_mitigation_uses_current_stage(monkeypatch):
    conductor = _conductor()
    conductor.current_stage_index = 1
    conductor.submission_stage = "mitigation"
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    response = asyncio.run(conductor_api.submit_solution(conductor_api.SubmitRequest(solution="fixed and verified")))

    assert response["stage"] == "mitigation"
    _wait_for_current_evaluation(conductor)


def test_legacy_empty_diagnosis_uses_idle_current_stage(monkeypatch):
    conductor = _conductor()
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    response = asyncio.run(conductor_api.submit_solution(conductor_api.SubmitRequest(solution="")))

    assert response["stage"] == "diagnosis"
    _wait_for_current_evaluation(conductor)


def test_http_api_waits_for_early_mitigation_and_rejects_duplicate_diagnosis(monkeypatch):
    diagnosis_gate = threading.Event()

    def evaluate_diagnosis(solution):
        diagnosis_gate.wait(2)
        conductor.results["Diagnosis"] = {"success": True, "submission": solution}

    def evaluate_mitigation(_solution):
        conductor.results["Mitigation"] = {"success": True}

    conductor = _conductor(evaluate_diagnosis, evaluate_mitigation)
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            diagnosis = await client.post(
                "/submit",
                json={"stage": "diagnosis", "solution": "diagnosis"},
            )
            assert diagnosis.status_code == 200
            assert diagnosis.json()["stage"] == "diagnosis"

            mitigation_request = asyncio.create_task(
                client.post(
                    "/submit",
                    json={"stage": "mitigation", "solution": ""},
                )
            )
            await asyncio.sleep(0.02)
            assert not mitigation_request.done()
            assert conductor._pending_submission_stages == {(1, "mitigation"): 1}

            duplicate = await client.post(
                "/submit",
                json={"stage": "diagnosis", "solution": "duplicate"},
            )
            assert duplicate.status_code == 409

            diagnosis_gate.set()
            await conductor.wait_for_submission_work(timeout=2)
            mitigation = await asyncio.wait_for(mitigation_request, timeout=2)
            assert mitigation.status_code == 200
            assert mitigation.json()["stage"] == "mitigation"

    try:
        asyncio.run(run())
    finally:
        diagnosis_gate.set()
        _wait_for_current_evaluation(conductor)

    assert conductor.results["Diagnosis"]["submission"] == "diagnosis"
    assert conductor.results["Mitigation"]["success"] is True


def test_early_mitigation_is_rejected_when_diagnosis_was_not_submitted(monkeypatch):
    conductor = _conductor()
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/submit",
                json={"stage": "mitigation", "solution": ""},
            )
            assert response.status_code == 409

    asyncio.run(run())
    assert conductor._pending_submission_stages == {}
    assert conductor.submission_stage == "diagnosis"


@pytest.mark.parametrize("stage", [None, "setup"])
def test_submission_before_an_agent_stage_is_rejected_without_leaking(monkeypatch, stage):
    conductor = _conductor()
    conductor.submission_stage = stage
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    async def run():
        transport = httpx.ASGITransport(app=conductor_api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/submit",
                json={"stage": "diagnosis", "solution": "diagnosis"},
            )
            assert response.status_code == 409

    asyncio.run(run())
    assert conductor._pending_submission_stages == {}


def test_api_cannot_observe_a_partial_stage_transition(monkeypatch):
    conductor = _conductor()
    conductor.waiting_for_agent = False
    conductor._evaluating = True
    monkeypatch.setattr(conductor_api, "_conductor", conductor)
    transition_started = threading.Event()

    def finish_diagnosis():
        with conductor._submission_lock:
            conductor._evaluating = False
            transition_started.set()
            time.sleep(0.05)
            conductor.current_stage_index = 1
            conductor.submission_stage = "mitigation"
            conductor.waiting_for_agent = True

    transition = threading.Thread(target=finish_diagnosis)
    transition.start()
    assert transition_started.wait(1)

    async def run():
        response = await conductor_api.submit_solution(conductor_api.SubmitRequest(stage="mitigation", solution=""))
        assert response["stage"] == "mitigation"

    asyncio.run(run())
    transition.join(timeout=1)
    _wait_for_current_evaluation(conductor)


def test_submission_wait_propagates_a_completed_evaluation_failure():
    conductor = _conductor()
    future = concurrent.futures.Future()
    future.set_exception(RuntimeError("cleanup failed"))
    conductor._submit_future = future

    with pytest.raises(RuntimeError, match="cleanup failed"):
        asyncio.run(conductor.wait_for_submission_work(timeout=1))
    assert conductor._submit_future is None


def test_submission_wait_times_out_while_a_request_remains_pending():
    conductor = _conductor()
    generation = conductor.register_pending_submission("mitigation")

    try:
        with pytest.raises(TimeoutError):
            asyncio.run(conductor.wait_for_submission_work(timeout=0.01))
    finally:
        conductor.unregister_pending_submission("mitigation", generation)


def test_abandoned_evaluator_cannot_publish_or_advance_late_result():
    gate = threading.Event()

    def evaluate(_solution):
        gate.wait(2)
        return {"success": True}

    conductor = _conductor(diagnosis_evaluation=evaluate)

    async def run():
        await conductor.submit("diagnosis", expected_stage="diagnosis")
        old_future = conductor._submit_future
        assert old_future is not None
        with pytest.raises(TimeoutError):
            await conductor.wait_for_submission_evaluations(timeout=0.01)
        conductor.abandon_submission_work()
        gate.set()
        old_future.result(timeout=2)

    asyncio.run(run())
    assert conductor.submission_stage == "aborted"
    assert "Diagnosis" not in conductor.results
    assert conductor._submit_future is None


def test_evaluation_and_cleanup_have_separate_deadlines():
    evaluation_started = threading.Event()
    release_evaluation = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def evaluate_mitigation(_solution):
        evaluation_started.set()
        release_evaluation.wait(2)
        return {"success": True}

    def recover_fault():
        cleanup_started.set()
        release_cleanup.wait(2)

    conductor = _conductor()
    conductor.stage_sequence = [{"name": "mitigation", "evaluation": evaluate_mitigation}]
    conductor.current_stage_index = 0
    conductor.submission_stage = "mitigation"
    conductor.problem = SimpleNamespace(
        recover_fault=recover_fault,
        app=SimpleNamespace(cleanup=lambda: None),
    )

    async def release_after(delay, event):
        await asyncio.sleep(delay)
        event.set()

    async def run():
        started_at = time.monotonic()
        await conductor.submit("", expected_stage="mitigation")
        assert evaluation_started.wait(1)

        evaluation_release = asyncio.create_task(release_after(0.12, release_evaluation))
        await conductor.wait_for_submission_evaluations(timeout=0.2)
        await evaluation_release

        assert conductor.submission_stage == "tearing_down"
        assert cleanup_started.wait(1)
        cleanup_future = conductor.finish_problem_in_background()
        assert cleanup_future is conductor._submit_future

        cleanup_release = asyncio.create_task(release_after(0.12, release_cleanup))
        await conductor.wait_for_submission_work(timeout=0.2)
        await cleanup_release
        return time.monotonic() - started_at

    elapsed = asyncio.run(run())

    assert elapsed > 0.2
    assert conductor.results["Mitigation"]["success"] is True
    assert conductor.missing_submission_stages() == []
    assert conductor.submission_stage == "done"
    assert conductor._submit_future is None


def test_abandoned_conductor_cannot_start_an_overlapping_attempt():
    conductor = _conductor()
    conductor.problem_id = "next-problem"
    conductor.abandon_submission_work()

    with pytest.raises(RuntimeError, match="cannot be reused"):
        asyncio.run(conductor.start_problem())


def test_worker_base_exception_becomes_controlled_evaluation_failure():
    conductor = _conductor(diagnosis_evaluation=lambda _solution: sys.exit("oracle exited"))

    async def run():
        await conductor.submit("diagnosis", expected_stage="diagnosis")
        with pytest.raises(RuntimeError, match="Evaluator terminated with SystemExit"):
            await conductor.wait_for_submission_work(timeout=1)

    asyncio.run(run())
    assert conductor._submit_future is None


def test_late_noise_restart_is_stopped_after_attempt_is_abandoned(monkeypatch):
    start_entered = threading.Event()
    release_start = threading.Event()

    class FakeNoiseManager:
        def __init__(self):
            self.running = False
            self.stages: list[str] = []

        def start(self):
            start_entered.set()
            release_start.wait(2)
            self.running = True

        def stop(self):
            self.running = False

        def set_stage(self, stage):
            self.stages.append(stage)

    noise = FakeNoiseManager()
    conductor = _conductor(diagnosis_evaluation=lambda _solution: {"success": True})
    conductor.config.enable_noise = True
    monkeypatch.setattr(conductor_module, "get_noise_manager", lambda: noise)

    worker = threading.Thread(
        target=conductor._submit_evaluate_and_advance,
        args=("diagnosis", conductor.stage_sequence[0], 1),
    )
    worker.start()
    assert start_entered.wait(1)
    conductor.abandon_submission_work()
    noise.stop()  # Emulate the driver's global shutdown after abandonment.
    release_start.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert noise.running is False
    assert noise.stages == []
    assert conductor.submission_stage == "aborted"


def test_incomplete_attempt_records_missing_stages_and_agent_exit():
    conductor = _conductor()
    conductor.results["Diagnosis"] = {"success": True}
    conductor.submission_stage = "mitigation"

    conductor.record_incomplete_attempt(
        "agent_exited_before_all_stages_completed",
        agent_return_code=0,
    )

    assert conductor.finalize_attempt_status() == "incomplete"
    assert conductor.results["incomplete_stage"] == "mitigation"
    assert conductor.results["missing_stages"] == "mitigation"
    assert conductor.results["agent_return_code"] == 0


def test_agent_exit_during_diagnosis_records_both_missing_stages():
    conductor = _conductor()

    conductor.record_incomplete_attempt(
        "agent_exited_before_all_stages_completed",
        agent_return_code=1,
    )

    assert conductor.finalize_attempt_status() == "incomplete"
    assert conductor.results["incomplete_stage"] == "diagnosis"
    assert conductor.results["missing_stages"] == "diagnosis,mitigation"
    assert conductor.results["agent_return_code"] == 1


def test_final_status_fails_safe_when_a_stage_result_is_missing():
    conductor = _conductor()
    conductor.results["Diagnosis"] = {"success": True}
    conductor.submission_stage = "done"

    assert conductor.finalize_attempt_status() == "incomplete"
    assert conductor.results["incomplete_reason"] == "missing_stage_results"
    assert conductor.results["incomplete_stage"] == "mitigation"


def test_final_status_is_complete_only_when_every_stage_has_a_result():
    conductor = _conductor()
    conductor.results["Diagnosis"] = {"success": True}
    conductor.results["Mitigation"] = {"success": False}

    assert conductor.finalize_attempt_status() == "complete"
    assert conductor.results["run_status"] == "complete"


def test_cleanup_failure_makes_a_fully_graded_attempt_incomplete():
    conductor = _conductor()
    conductor.results["Diagnosis"] = {"success": True}
    conductor.results["Mitigation"] = {"success": True}
    conductor.results["cleanup_failed"] = True

    assert conductor.finalize_attempt_status() == "incomplete"
    assert conductor.results["incomplete_reason"] == "cleanup_failed"


def test_cleanup_continues_after_recovery_error_and_reaches_terminal_state():
    conductor = _conductor()
    app_cleaned = threading.Event()

    def recover_fault():
        raise RuntimeError("recovery failed")

    conductor.problem = SimpleNamespace(
        recover_fault=recover_fault,
        app=SimpleNamespace(cleanup=app_cleaned.set),
    )
    conductor._baseline_captured = False
    conductor.submission_stage = "tearing_down"

    conductor._cleanup_sync()

    assert app_cleaned.is_set()
    assert conductor.submission_stage == "done"
    assert conductor.results["cleanup_failed"] is True
    assert "recover_fault: RuntimeError: recovery failed" in conductor.results["cleanup_error"]


def test_abandoned_cleanup_does_not_run_later_cleanup_phases():
    conductor = _conductor()
    recovery_started = threading.Event()
    release_recovery = threading.Event()
    app_cleanup_ran = threading.Event()
    reconcile_ran = threading.Event()

    def recover_fault():
        recovery_started.set()
        release_recovery.wait(2)

    conductor.problem = SimpleNamespace(
        recover_fault=recover_fault,
        app=SimpleNamespace(cleanup=app_cleanup_ran.set),
    )
    conductor._baseline_captured = True
    conductor.cluster_state = SimpleNamespace(reconcile_to_baseline=lambda: reconcile_ran.set() or {})
    conductor.submission_stage = "tearing_down"

    cleanup = threading.Thread(target=conductor._cleanup_sync, args=(1,))
    cleanup.start()
    assert recovery_started.wait(1)
    conductor.abandon_submission_work()
    release_recovery.set()
    cleanup.join(timeout=2)

    assert not cleanup.is_alive()
    assert not app_cleanup_ran.is_set()
    assert not reconcile_ran.is_set()
    assert conductor.submission_stage == "aborted"


def test_background_safety_cleanup_can_be_bounded_and_abandoned():
    conductor = _conductor()
    recovery_started = threading.Event()
    release_recovery = threading.Event()
    app_cleanup_ran = threading.Event()

    def recover_fault():
        recovery_started.set()
        release_recovery.wait(2)

    conductor.problem = SimpleNamespace(
        recover_fault=recover_fault,
        app=SimpleNamespace(cleanup=app_cleanup_ran.set),
    )

    async def run():
        future = conductor.finish_problem_in_background()
        assert recovery_started.wait(1)
        with pytest.raises(TimeoutError):
            await conductor.wait_for_submission_work(timeout=0.01)
        conductor.abandon_submission_work()
        release_recovery.set()
        future.result(timeout=2)

    asyncio.run(run())
    assert not app_cleanup_ran.is_set()
    assert conductor.submission_stage == "aborted"
    assert conductor._submit_future is None


def test_orphaned_teardown_cannot_be_reported_as_successful_cleanup():
    conductor = _conductor()
    conductor.submission_stage = "tearing_down"
    conductor._submit_future = None

    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        conductor.finish_problem_in_background()


def test_no_stage_path_starts_bounded_background_cleanup():
    conductor = _conductor()
    recovery_started = threading.Event()
    release_recovery = threading.Event()

    def recover_fault():
        recovery_started.set()
        release_recovery.wait(2)

    conductor.problem = SimpleNamespace(
        recover_fault=recover_fault,
        app=SimpleNamespace(cleanup=lambda: None),
    )
    conductor.stage_sequence = []

    started_at = time.monotonic()
    Conductor._advance_to_next_stage(conductor)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert recovery_started.wait(1)
    future = conductor._submit_future
    assert future is not None
    conductor.abandon_submission_work()
    release_recovery.set()
    future.result(timeout=2)


def test_new_generation_uses_setup_state_before_early_setup_failure(monkeypatch):
    recovered = threading.Event()
    app_cleaned = threading.Event()
    new_problem = SimpleNamespace(
        recover_fault=recovered.set,
        app=SimpleNamespace(cleanup=app_cleaned.set),
    )
    conductor = _conductor()
    conductor.problem_id = "problem"
    conductor.submission_stage = "done"
    conductor.problems = SimpleNamespace(get_problem_instance=lambda _problem_id: new_problem)
    conductor.dependency_check = lambda _binaries: (_ for _ in ()).throw(RuntimeError("missing dependency"))
    monkeypatch.setattr(conductor_module, "DetectionOracle", lambda _problem: object())

    with pytest.raises(RuntimeError, match="missing dependency"):
        asyncio.run(conductor.start_problem())

    assert conductor.submission_stage == "setup"
    future = conductor.finish_problem_in_background()
    future.result(timeout=2)

    assert recovered.is_set()
    assert app_cleaned.is_set()
    assert conductor.submission_stage == "done"


def test_teardown_failure_stays_incomplete_even_when_stage_results_exist():
    conductor = _conductor()
    conductor.results["Diagnosis"] = {"success": True}
    conductor.results["Mitigation"] = {"success": True}
    conductor.submission_stage = "tearing_down"

    conductor.record_incomplete_attempt("evaluation_timeout_after_agent_exit", agent_return_code=0)

    assert conductor.finalize_attempt_status() == "incomplete"
    assert conductor.results["incomplete_stage"] == "tearing_down"
    assert conductor.results["missing_stages"] == ""
