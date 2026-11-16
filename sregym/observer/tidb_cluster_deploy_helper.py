import subprocess
from pathlib import Path

from sregym.service.apps.tidb_cluster_operator import TiDBClusterDeployer


class TiDBClusterDeployHelper:
    _ready = False

    @classmethod
    def running_cluster(self):
        # The between-problem reset can remove the operator, CRDs and cluster, so re-check.
        if self._ready:
            found = subprocess.run(
                ["kubectl", "get", "tidbcluster", "basic", "-n", "tidb-cluster"], capture_output=True
            )
            self._ready = found.returncode == 0
        if not self._ready:
            base_dir = Path(__file__).parent.parent
            meta_path = base_dir / "service" / "metadata" / "tidb_metadata.json"
            print("Starting TiDB Cluster...")
            deployer = TiDBClusterDeployer(str(meta_path))
            deployer.deploy_all()
            self._ready = True
            print("TiDB Cluster is running.")
        else:
            print("TiDB Cluster is already running.")
