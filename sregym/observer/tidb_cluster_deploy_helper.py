import os
from pathlib import Path

from sregym.service.apps.tidb_cluster_operator import TiDBClusterDeployer


class TiDBClusterDeployHelper:
    _ready = False

    @classmethod
    def running_cluster(self):
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
