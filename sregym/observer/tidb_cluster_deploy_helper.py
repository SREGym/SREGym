from pathlib import Path

from sregym.service.apps.tidb_cluster_operator import TiDBClusterDeployer


class TiDBClusterDeployHelper:
    @classmethod
    def running_cluster(cls):
        # Always deploy: the between-problem reset may have removed TiDB, or a kept cluster
        # may still carry the last problem's fault. deploy_all() is safe on a healthy cluster
        # and reseeds the tables from scratch.
        meta_path = Path(__file__).parent.parent / "service" / "metadata" / "tidb_metadata.json"
        print("Starting TiDB Cluster...")
        TiDBClusterDeployer(str(meta_path)).deploy_all()
        print("TiDB Cluster is running.")
