import shlex

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Overwrite everything after the 4 KiB WiredTiger file header with random bytes. The
# catalog is read on every mongod start, so the corruption surfaces deterministically
# as a checksum failure instead of waiting for a query to touch a damaged page.
CORRUPT_WIREDTIGER_FILES = """
for f in /data/db/_mdb_catalog.wt /data/db/collection-*.wt; do
    size=$(stat -c %s "$f")
    [ "$size" -gt 4096 ] || continue
    dd if=/dev/urandom of="$f" bs=4096 seek=1 count=$((size / 4096 - 1)) conv=notrunc 2>/dev/null
done
"""


class SilentDataCorruption(Problem):
    """Corrupt a MongoDB volume's on-disk data files, as failing storage would.

    Runs entirely through the Kubernetes API, so it needs no privileges or kernel
    features on the host.
    """

    def __init__(self, target_deploy: str = "mongodb-geo", namespace: str = "hotel-reservation"):
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy

        super().__init__(app=HotelReservation())

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.deploy}",
            namespace=self.namespace,
            description=(
                "The data files on the persistent volume backing this MongoDB workload were silently corrupted "
                "at the storage layer, without any I/O errors being reported. WiredTiger detects the damage as "
                "checksum failures when it reads the affected pages, and mongod aborts with a fatal assertion on "
                "every start, leaving the database in CrashLoopBackOff until its data is restored."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print(f"[SDC] Corrupting WiredTiger data files of {self.deploy}")
        deploy = f"-n {self.namespace} deployment/{self.deploy}"
        self.kubectl.exec_command_checked(
            f"kubectl exec {deploy} -- sh -ec {shlex.quote(CORRUPT_WIREDTIGER_FILES)}", timeout=120
        )
        # mongod has the damaged pages cached; restart it so they are read back from disk.
        # A clean shutdown only rewrites dirty pages, and nothing writes to this data.
        self.kubectl.exec_command_checked(f"kubectl scale {deploy} --replicas=0")
        self.kubectl.exec_command_checked(
            f"kubectl wait -n {self.namespace} --for=delete pod -l io.kompose.service={self.deploy} --timeout=180s",
            timeout=200,
        )
        self.kubectl.exec_command_checked(f"kubectl scale {deploy} --replicas=1")
        print("[SDC] Silent data corruption injection complete")

    @mark_fault_injected
    def recover_fault(self):
        print("[SDC] Redeploying the app with freshly seeded data")
        self.app.cleanup()
        self.app.deploy()
        self.app.start_workload()
        print("[SDC] ✅ Recovery complete")
