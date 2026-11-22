"""Lock leaks for Slack Spine."""

from __future__ import annotations

import time

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.apps.slack_spine.base import SlackSpineProblem
from sregym.conductor.problems.apps.slack_spine.components import TERMINATE_HOLDERS_SQL, SlackLeg
from sregym.service.apps.helpers import image_reference


class LeakedSequencerLock(SlackLeg):
    """The deployed release leaks a ``channel_seq`` row lock from an idle transaction.

    Incident Arena ships this build as *the* release (Tier-2 image fault): a
    boot transaction takes ``SELECT ... FOR UPDATE`` on chan-0's cursor and
    never commits, so chan-0 writes block and starve the message pool. A
    restart re-arms it; the release must stay, so the repair is operational.
    """

    component = "message/message.sequencer"
    RECURRENCE_WAIT_S = 45

    def deploy_values(self):
        # Native builds of the same task release, shared by every application role.
        return {"images": {"app": image_reference("slack-spine-app-v2")}}

    def inject(self) -> None:
        # The release is the fault; wait until its leaked lock is held.
        self.wait_for(lambda: self.holder_count() > 0, timeout_s=600, what="a leaked channel_seq lock holder")

    def recover(self) -> None:
        self.slack.psql(TERMINATE_HOLDERS_SQL)
        self.slack.psql("ALTER SYSTEM SET idle_in_transaction_session_timeout = '10s'")
        self.slack.psql("SELECT pg_reload_conf()")

    def checks(self, phase: str) -> list[CheckResult]:
        holders = self.holder_count()
        return [CheckResult(f"no_channel_seq_lock_holder@{phase}", holders == 0, detail={"holders": holders})]

    def challenges(self):
        def restart() -> CheckResult:
            try:
                self.slack.rollout_restart("deployment", "svc-message")
            except Exception as exc:
                return CheckResult(
                    "message_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            time.sleep(self.RECURRENCE_WAIT_S)
            holders = self.holder_count()
            return CheckResult(
                "no_lock_holder_after_restart",
                holders == 0,
                reason="repair_not_durable",
                detail={"holders": holders, "waited_s": self.RECURRENCE_WAIT_S},
            )

        return [restart]

    def describe(self) -> str:
        return (
            "The deployed slack-app release opens a boot transaction that runs SELECT ... FOR UPDATE on chan-0's "
            "channel_seq row and stays idle in transaction, so chan-0 sends block and exhaust the message pool."
        )


class SessionHandoffLock(SlackLeg):
    """The deployed release's session-scoped sequencer handoff leaks a cohort's row lock.

    The message role persists its checkpoint/handoff mode in ``app_kv_state``.
    In ``session`` mode a retry-reused database lease keeps one seeded channel
    cohort's ``channel_seq`` row locked in an idle transaction. The durable
    repair persists ``request`` mode (``PUT /admin/checkpoint``, write-only)
    and reloads, which restarts the role's process. ``GET /internal/s13/cp``
    reports the persisted mode and the generation the running process booted.
    """

    component = "message/message.sequencer"
    RECURRENCE_WAIT_S = 30

    def deploy_values(self):
        return {"images": {"app": image_reference("slack-spine-app-v3")}}

    def _mode(self) -> dict:
        return self.slack.admin("message", "/internal/s13/cp")

    def inject(self) -> None:
        self.wait_for(lambda: self.holder_count() > 0, timeout_s=600, what="a leaked channel_seq lock holder")

    def recover(self) -> None:
        self.slack.admin("message", "/admin/checkpoint", "PUT", {"mode": "request"})
        self.slack.admin("message", "/admin/reload", "POST")
        self.slack.psql(TERMINATE_HOLDERS_SQL)

    def _state_checks(self, phase: str, reason: str = "fault_still_present") -> list[CheckResult]:
        mode = self._mode()
        holders = self.holder_count()
        return [
            CheckResult(
                f"request_mode_persisted@{phase}",
                mode.get("mode") == "request" and mode.get("persisted", True) is not False,
                reason=reason,
                detail=mode,
            ),
            # A PUT without a reload persists the mode but leaves the process on
            # the session handoff it booted with.
            CheckResult(
                f"request_mode_live@{phase}",
                int(mode.get("boot_generation") or 0) >= int(mode.get("generation") or 0),
                reason=reason,
                detail=mode,
            ),
            CheckResult(
                f"no_channel_seq_lock_holder@{phase}", holders == 0, reason=reason, detail={"holders": holders}
            ),
        ]

    def checks(self, phase: str) -> list[CheckResult]:
        return self._state_checks(phase)

    def challenges(self):
        def restart() -> CheckResult:
            try:
                self.slack.rollout_restart("deployment", "svc-message")
            except Exception as exc:
                return CheckResult(
                    "message_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            time.sleep(self.RECURRENCE_WAIT_S)
            post = self._state_checks("post_restart", reason="repair_not_durable")
            failed = [r.name for r in post if not r.passed]
            return CheckResult(
                "handoff_repair_survives_restart",
                not failed,
                reason="repair_not_durable",
                detail={"checks": [r.as_dict() for r in post]},
            )

        return [restart]

    def describe(self) -> str:
        return (
            "The deployed slack-app release runs the message sequencer handoff in session mode; a retry-reused "
            "lease leaves one channel cohort's channel_seq row locked in an idle transaction."
        )


class SlackSeqLockLeak(SlackSpineProblem):
    """Incident Arena 018: the release leaks a chan-0 channel_seq row lock (idle in transaction)."""

    TASK = "018--slack-spine--09-I1-seq-lock-leak-0b7c2973"
    PROBLEM_ID = "slack_seq_lock_leak"
    # The faulty release is deployed from the start.
    HEALTHY_BASELINE = False
    # The reference repair bounds idle transactions server-wide.
    ALLOWED_PG_SETTINGS = ("idle_in_transaction_session_timeout",)

    def build_legs(self):
        return [LeakedSequencerLock()]


class SlackDistractorVolumeSeqLock(SlackSpineProblem):
    """Incident Arena 019: session-scoped sequencer handoff leaks a cohort lock under heavy log noise."""

    TASK = "019--slack-spine--13-P1-distractor-volume-shell-f73987d7"
    PROBLEM_ID = "slack_distractor_volume_seq_lock"
    # The faulty release is deployed from the start.
    HEALTHY_BASELINE = False

    def build_legs(self):
        return [SessionHandoffLock()]
