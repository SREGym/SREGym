"""TiDBClusterDeployHelper deploys TiDB on every call.

A cached "ready" flag from an earlier problem skipped the deploy after the
between-problem reset had removed TiDB, or reused a cluster still carrying the
previous problem's fault.
"""

from unittest.mock import Mock

from sregym.observer import tidb_cluster_deploy_helper as helper_module
from sregym.observer.tidb_cluster_deploy_helper import TiDBClusterDeployHelper


def test_deploys_on_every_call(monkeypatch):
    # Arrange
    deployer = Mock()
    monkeypatch.setattr(helper_module, "TiDBClusterDeployer", Mock(return_value=deployer))

    # Act
    TiDBClusterDeployHelper.running_cluster()
    TiDBClusterDeployHelper.running_cluster()

    # Assert
    assert deployer.deploy_all.call_count == 2
