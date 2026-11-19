"""TiDBClusterDeployHelper redeploys when its cached "ready" cluster is gone.

The between-problem reset removes the TiDB operator, CRDs and cluster, so a cached
ready flag from an earlier problem must not skip the redeploy.
"""

import subprocess
from unittest.mock import Mock

import pytest

from sregym.observer import tidb_cluster_deploy_helper as helper_module
from sregym.observer.tidb_cluster_deploy_helper import TiDBClusterDeployHelper


@pytest.fixture
def deployer(monkeypatch):
    deployer = Mock()
    monkeypatch.setattr(helper_module, "TiDBClusterDeployer", Mock(return_value=deployer))
    monkeypatch.setattr(TiDBClusterDeployHelper, "_ready", True)
    return deployer


def _cluster_lookup(monkeypatch, returncode):
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess("kubectl", returncode)))


def test_redeploys_when_the_cached_cluster_was_removed(monkeypatch, deployer):
    # Arrange
    _cluster_lookup(monkeypatch, returncode=1)

    # Act
    TiDBClusterDeployHelper.running_cluster()

    # Assert
    deployer.deploy_all.assert_called_once()


def test_reuses_the_cluster_when_it_still_exists(monkeypatch, deployer):
    # Arrange
    _cluster_lookup(monkeypatch, returncode=0)

    # Act
    TiDBClusterDeployHelper.running_cluster()

    # Assert
    deployer.deploy_all.assert_not_called()
