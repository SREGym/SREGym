"""A restore from backup rolled back hours of users' work; put it back while they keep working.

After Linear's incident of 24 January 2024 (its post-mortem, quoted below). A
migration's TRUNCATE ... CASCADE emptied production tables: "CASCADE simply
truncates the entire contents of any tables that have foreign keys to the
table". Deleting rows that way "won't create sync packets and thus won't
update the cache or clients". Linear went into maintenance mode, "created a
new backup to capture all the sync packets created up until that point", and
restored its last full backup, which rolled back about five hours of work.
Then, with users back, "a restoration script ... reapplied the actions by
users, integrations, API calls, and Linear automations" from that sync log,
skipping what conflicted ("We did not want to override changes made by
users"; "If an update for an entity was archived or deleted, those updates
were skipped") and listing what it skipped on each workspace's recovery page.

The agent takes over where that script began. Tracker is back up on the
backup, users are working, and the lost window exists only in the copy of the
sync log. Three things here are ours, not the post-mortem's: issue numbers
come from a per-team counter that the restore rolled back, so new issues
reuse the numbers lost issues had; the lost window is minutes long, not
hours; and some users, finding a comment or an issue of theirs gone, have
made it again before the agent takes over. The post-mortem's nearest case is
content that "already existed" because it "had been recently created", which
Linear left as its users had made it and listed.

The properties this problem tests:

- The lost work has to go back into a system of record that its users are
  changing while it goes back, some of them the very fields that lost work.
  A plan made from the sync log is out of date by the time it is applied;
  whatever is decided about a field has to be decided when it is written.
  Writing behind tracker-api (SQL) changes rows the cache and users' apps
  never hear of, and taking Tracker down to do it cleanly takes users
  offline again.
- What a user has made again has a new id, and tracker-api accepts the lost
  one beside it. The handover note does not say that customers have started
  putting lost work back themselves; the agent has to see it in the data.
  An issue created again has the lost issue's team and number as well as its
  title and text, so a look at which issue took a lost issue's number finds it. A comment
  posted again shares no key with the lost one: only what the two say shows
  they are the same comment. The lost one stays out and is listed on the
  recovery page, like everything else that is not put back.
"""

import time
from datetime import UTC, datetime

from sregym.conductor.oracles.apps.tracker.restore_replay import RestoreReplayOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.tracker.application import DB, MIGRATE_JOB, MIGRATIONS, TrackerApplication
from sregym.utils.decorators import mark_fault_injected

#: Steady work before the agent takes over, so the handover is taken from a tracker in use.
SETTLE_SECONDS = 20


def clock():
    return datetime.now(UTC).strftime("%H:%M")


class TrackerRestoreReplayProblem(Problem):
    application_class = TrackerApplication
    run_default_workload = False
    cleanup_timeout_seconds = 900
    recommended_agent_timeout_seconds = 1800

    def __init__(self, scale_tier="small"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service = DB
        #: The last sync id in the backup; later ids in the kept sync log are the lost window.
        self.backup_sync_id = None
        #: The lost window's sync actions, in order.
        self.lost = []
        #: The database when the agent took over, and the users' clock then.
        self.handover = None
        self.handover_at = None
        self.root_cause = self.build_structured_root_cause(
            component=DB,
            namespace=self.namespace,
            description=f"Migration 0042 (the Job {MIGRATE_JOB}, from the ConfigMap {MIGRATIONS}) ran TRUNCATE TABLE "
            "thread CASCADE. The comment table references thread, so the cascade emptied it: every comment in every "
            "workspace was deleted from tracker-db, with no sync action, so the cache and users' apps went on "
            "showing them. The on-call engineer then put Tracker in maintenance mode and restored the last full "
            "backup, which predates the migration. Everything users did between that backup and the restore (new "
            "issues, changes to issues, comments) is therefore missing from the tracker database; it survives only "
            "as sync actions in the copy of the sync log kept in the prerestore database on tracker-db. Users have "
            "been working since the restore, and some have changed the same issues again, so the lost work has to "
            "be put back through tracker-api alongside their new changes, without overwriting them and without "
            "taking Tracker offline.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = RestoreReplayOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        print("[1/6] The last full backup")
        app.backup()
        self.backup_sync_id = app.last_sync_id()
        print("[2/6] Users work on: the lost window begins")
        print(f"      {app.users_post('/phase?name=lost&part=1')}")
        print("[3/6] Migration 0042: TRUNCATE ... CASCADE empties the comment table")
        app.run_migration()
        print(f"      {app.users_post('/phase?name=lost&part=2')}")
        print("[4/6] Maintenance mode; the sync log is kept; the backup is restored")
        down = clock()
        app.set_maintenance(True)
        app.keep_sync_log()
        app.restore_backup()
        app.restart_api()  # a new process, and the only one: an empty cache, and out of maintenance mode
        self.lost = app.lost_actions(self.backup_sync_id)
        if not self.lost:
            raise RuntimeError("the kept sync log holds no lost window")
        print(f"[5/6] Users are back ({len(self.lost)} sync actions lost)")
        # Their apps download the restored tracker. Asked again if the new tracker-api is not reachable yet.
        app.wait_for(lambda: app.users_post("/phase?name=reset"), 180, 2, "users' apps to download the tracker")
        after = app.users_post("/phase?name=after")
        print(f"      {after}")
        for what, key in (("reposted", "reposts"), ("recreated", "recreates")):
            counts = [done.get(what, 0) for done in after["workspaces"].values()]
            if any(count != app.tier[key] for count in counts):
                # An old hidden image would leave this out silently, and the run would test something else.
                raise RuntimeError(f"users {what} {counts} lost items, not {app.tier[key]} in each workspace")
        app.users_post("/phase?name=live")
        time.sleep(SETTLE_SECONDS)
        print("[6/6] Handover")
        self.handover = app.snapshot()
        self.handover_at = app.users_clock()
        app.description = app.handover_description(down)

    @mark_fault_injected
    def recover_fault(self):
        """Put the lost window back with the reference repair, users still at work.

        The lost window is data, so recovering is repairing: the same replay through tracker-api that the
        controls and the tests use, which leaves alone what users have changed or made again. It repeats
        nothing that is already back.

        This must not raise. It also runs when an attempt is cleaned up, and a failed cleanup marks the
        attempt incomplete, which would turn an attempt that left Tracker down into an inconclusive one.
        """
        if not self.lost:
            return
        try:
            print(f"reference repair: {self.app.replay(self.lost, self.backup_sync_id)}")
        except Exception as error:  # noqa: BLE001 - see above
            print(f"reference repair did not run to the end: {error}")
