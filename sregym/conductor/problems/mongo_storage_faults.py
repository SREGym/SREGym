"""Storage faults that damage a MongoDB volume's WiredTiger files directly.

They need no privileges or kernel features on the host, only the Kubernetes API,
so they run on kind and in sandboxes.
"""

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Hidden from the agent, so the pod that edits the volume leaves no trace in the app namespace.
FAULT_POD_NAMESPACE = "khaos"

# The catalog is read on every mongod start, so damage to it surfaces deterministically.
WIREDTIGER_FILES = "_mdb_catalog.wt collection-*.wt"


class MongoStorageFault(Problem):
    """Stop a MongoDB deployment, run SCRIPT in its data directory on the node, then start it again.

    Editing the files while mongod runs does not work: its clean shutdown writes them again.
    """

    SCRIPT: str
    DESCRIPTION: str

    def __init__(self, target_deploy: str = "mongodb-geo", namespace: str = "hotel-reservation"):
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy

        super().__init__(app=HotelReservation())

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.deploy}", namespace=self.namespace, description=self.DESCRIPTION
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    def _volume_location(self) -> tuple[str, str]:
        """Return the node and host path of the deployment's data volume."""
        core = self.kubectl.core_v1_api
        pod = core.list_namespaced_pod(self.namespace, label_selector=f"io.kompose.service={self.deploy}").items[0]
        claim = next(v.persistent_volume_claim.claim_name for v in pod.spec.volumes if v.persistent_volume_claim)
        pv = core.read_persistent_volume(
            core.read_namespaced_persistent_volume_claim(claim, self.namespace).spec.volume_name
        )
        path = pv.spec.local.path if pv.spec.local else pv.spec.host_path.path
        return pod.spec.node_name, path

    @mark_fault_injected
    def inject_fault(self):
        print(f"[{self.__class__.__name__}] Damaging WiredTiger data files of {self.deploy}")
        node, path = self._volume_location()
        deploy = f"-n {self.namespace} deployment/{self.deploy}"

        self.kubectl.exec_command_checked(f"kubectl scale {deploy} --replicas=0")
        self.kubectl.exec_command_checked(
            f"kubectl wait -n {self.namespace} --for=delete pod -l io.kompose.service={self.deploy} --timeout=180s",
            timeout=200,
        )
        self.kubectl.create_namespace_if_not_exist(FAULT_POD_NAMESPACE)
        self.kubectl.run_node_script_pod(
            node_name=node,
            namespace=FAULT_POD_NAMESPACE,
            script=f'set -e\ncd "/host{path}"\n{self.SCRIPT}',
            name_prefix="sregym-storage-fault",
        )
        self.kubectl.exec_command_checked(f"kubectl scale {deploy} --replicas=1")
        print(f"[{self.__class__.__name__}] Injection complete")

    @mark_fault_injected
    def recover_fault(self):
        print(f"[{self.__class__.__name__}] Redeploying the app with freshly seeded data")
        self.app.cleanup()
        self.app.deploy()
        self.app.start_workload()
        print(f"[{self.__class__.__name__}] ✅ Recovery complete")


class SilentDataCorruption(MongoStorageFault):
    """Blocks are still readable but their contents are wrong, so WiredTiger checksums fail."""

    SCRIPT = f"""
for f in {WIREDTIGER_FILES}; do
    size=$(stat -c %s "$f")
    [ "$size" -gt 4096 ] || continue
    dd if=/dev/urandom of="$f" bs=4096 seek=1 count=$((size / 4096 - 1)) conv=notrunc 2>/dev/null
done
"""
    DESCRIPTION = (
        "The data files on the persistent volume backing this MongoDB workload were silently corrupted "
        "at the storage layer, without any I/O errors being reported. WiredTiger detects the damage as "
        "checksum failures when it reads the affected pages, and mongod aborts with a fatal assertion on "
        "every start, leaving the database in CrashLoopBackOff until its data is restored."
    )


class LatentSectorError(MongoStorageFault):
    """Blocks past the file header can no longer be read at all, so WiredTiger reads come back short."""

    # Keep only the 4 KiB WiredTiger file header, so every data block becomes unreadable.
    SCRIPT = f"""
for f in {WIREDTIGER_FILES}; do
    truncate -s 4096 "$f"
done
"""
    DESCRIPTION = (
        "The storage backing this MongoDB workload developed latent sector errors: regions of its data "
        "files can no longer be read. WiredTiger's reads of those blocks fail (pread cannot read the "
        "requested bytes), and mongod aborts with a fatal assertion on every start, leaving the database "
        "in CrashLoopBackOff until its data is restored."
    )
