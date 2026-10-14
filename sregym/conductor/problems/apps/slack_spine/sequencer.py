"""Sequencer for Slack Spine."""

from __future__ import annotations

from sregym.conductor.oracles.incident_arena import CheckResult
from sregym.conductor.problems.apps.slack_spine.base import SlackSpineProblem
from sregym.conductor.problems.apps.slack_spine.components import SlackLeg


class SequencerMode(SlackLeg):
    """``SEQUENCER_MODE=rmw`` on svc-message: a non-atomic read-modify-write allocator.

    Concurrent same-channel sends read the same cursor and persist the same
    sequence number, silently corrupting history order (no 5xx). A repair must
    select the atomic allocator durably and losslessly re-sequence the rows.
    """

    component = "message/message.sequencer"

    def __init__(self) -> None:
        super().__init__()
        self.message_count = 0

    def _count(self) -> int:
        return int(self.slack.psql("SELECT count(*) FROM messages").strip() or 0)

    def capture_baseline(self) -> None:
        self.message_count = self._count()

    def inject(self) -> None:
        self.slack.kubectl.exec_command_checked(
            f"kubectl set env deployment/svc-message -n {self.namespace} -c app SEQUENCER_MODE=rmw", timeout=60
        )
        self.slack.wait_rollout("deployment", "svc-message")

    def recover(self) -> None:
        self.slack.kubectl.exec_command_checked(
            f"kubectl set env deployment/svc-message -n {self.namespace} -c app SEQUENCER_MODE-", timeout=60
        )
        self.slack.wait_rollout("deployment", "svc-message")
        # Re-sequence only once no rmw pod can still allocate a duplicate.
        self.slack.wait_terminated(self.slack.role_selector("message"))
        self.slack.psql(
            "BEGIN; SELECT 1 FROM channel_seq FOR UPDATE; "
            "WITH renum AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY channel_id ORDER BY id) AS new_seq FROM messages) "
            "UPDATE messages m SET seq = r.new_seq FROM renum r WHERE m.id = r.id AND m.seq <> r.new_seq; "
            "UPDATE channel_seq cs SET last_seq = mx.max_seq FROM (SELECT channel_id, max(seq) AS max_seq "
            "FROM messages GROUP BY channel_id) mx WHERE cs.channel_id = mx.channel_id AND cs.last_seq <> mx.max_seq; "
            "COMMIT;"
        )

    def _mode_checks(self, phase: str, reason: str = "fault_still_present") -> list[CheckResult]:
        env_mode = self.deployment_env("svc-message").get("SEQUENCER_MODE")
        live_mode = self.slack.admin("message", "/admin/sequencer").get("mode")
        return [
            CheckResult(
                f"sequencer_atomic_persisted@{phase}",
                env_mode in (None, "", "atomic"),
                reason=reason,
                detail={"SEQUENCER_MODE": env_mode},
            ),
            CheckResult(
                f"sequencer_atomic_live@{phase}", live_mode == "atomic", reason=reason, detail={"mode": live_mode}
            ),
        ]

    def checks(self, phase: str) -> list[CheckResult]:
        results = self._mode_checks(phase)
        duplicates = int(
            self.slack.psql(
                "SELECT count(*) FROM (SELECT channel_id, seq FROM messages GROUP BY channel_id, seq HAVING count(*) > 1) d"
            ).strip()
            or 0
        )
        sparse = int(
            self.slack.psql(
                "SELECT count(*) FROM (SELECT channel_id FROM messages GROUP BY channel_id "
                "HAVING min(seq) <> 1 OR max(seq) <> count(*)) g"
            ).strip()
            or 0
        )
        cursors = int(
            self.slack.psql(
                "SELECT count(*) FROM channel_seq cs JOIN (SELECT channel_id, max(seq) AS mx FROM messages "
                "GROUP BY channel_id) m USING (channel_id) WHERE cs.last_seq <> m.mx"
            ).strip()
            or 0
        )
        count = self._count()
        results += [
            CheckResult(f"no_duplicate_sequences@{phase}", duplicates == 0, detail={"duplicate_pairs": duplicates}),
            CheckResult(f"sequences_dense@{phase}", sparse == 0, detail={"non_dense_channels": sparse}),
            CheckResult(f"cursors_match_history@{phase}", cursors == 0, detail={"mismatched_cursors": cursors}),
            CheckResult(
                f"accepted_messages_preserved@{phase}",
                count >= self.message_count,
                reason="unsafe_repair",
                detail={"before": self.message_count, "after": count},
            ),
        ]
        return results

    def challenges(self):
        def restart() -> CheckResult:
            try:
                self.slack.rollout_restart("deployment", "svc-message")
            except Exception as exc:
                return CheckResult(
                    "message_restart_challenge", False, reason="challenge_failed", detail={"error": str(exc)}
                )
            post = self._mode_checks("post_restart", reason="repair_not_durable")
            failed = [r.name for r in post if not r.passed]
            return CheckResult(
                "sequencer_survives_restart", not failed, reason="repair_not_durable", detail={"failed": failed}
            )

        return [restart]

    def describe(self) -> str:
        return "SEQUENCER_MODE=rmw on the svc-message Deployment (non-atomic read-modify-write channel_seq allocation)."


class SlackSplitSequencer(SlackSpineProblem):
    """Incident Arena 007: non-atomic sequencer silently duplicates per-channel sequence numbers."""

    TASK = "007--slack-spine--06-F3-split-sequencer-eef5b438"
    PROBLEM_ID = "slack_split_sequencer"

    def build_legs(self):
        return [SequencerMode()]
