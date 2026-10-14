"""Slack Spine: feature flags."""

from __future__ import annotations

from sregym.conductor.oracles.apps.slack_spine.feature_flags import SequencerModeOracle
from sregym.conductor.problems.apps.slack_spine.helpers import APP, wait_rollout
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port
from sregym.utils.decorators import mark_fault_injected


class FeatureFlagLatentBugSlack(Problem):
    """Analogue: a config flag (``SEQUENCER_MODE=rmw``) activates a latent code path in svc-message.

    The original flips ``SEARCH_BACKEND_VERSION`` and the frontend image's
    dormant path fails every search. Slack Spine's image ships a dormant
    non-atomic read-modify-write channel_seq allocator behind
    ``SEQUENCER_MODE``; with the flag on and concurrent sends arriving it
    hands out duplicate sequence numbers while every pod stays Running and
    every send returns 201. (Incident Arena task 007 uses the same toggle.)
    """

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        self.flag_key = "SEQUENCER_MODE"
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"A configuration change set `{self.flag_key}=rmw` on the `{faulty_service}` Deployment, activating "
                "a dormant code path compiled into the message image: instead of the atomic `INSERT ... ON CONFLICT "
                "DO UPDATE ... RETURNING` channel_seq increment, sends read `channel_seq.last_seq`, hold the "
                "transaction, and write `last_seq + 1` back (a non-atomic read-modify-write). With the flag on and "
                "concurrent sends to the same channel, sends read the same cursor and persist duplicate per-channel "
                "sequence numbers, so channel history order is silently corrupted. Every pod stays Running and "
                "sends still return 201, so the failure is visible only in the data (duplicate `messages.seq` per "
                "channel) and in svc-message GET /admin/sequencer reporting `rmw`. The fix is to revert the flag "
                "(unset it or set `atomic`) on the Deployment and roll svc-message out so the atomic allocator is "
                "live again."
            ),
            oracle_factory=SequencerModeOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} -c app {self.flag_key}=rmw"
        )
        wait_rollout(self, self.faulty_service, timeout_s=300, check=True)
        print(f"{self.flag_key}=rmw on {self.faulty_service}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} -c app {self.flag_key}-"
        )
        wait_rollout(self, self.faulty_service, timeout_s=300)
