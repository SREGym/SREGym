"""
Test suite for the CloudLab provisioner functionality.

This module contains integration tests that test the core functionality of the CloudLab
provisioner, including cluster provisioning, user management, and automatic cleanup.

NOTE: These tests are live and will provision real CloudLab clusters.
You must have your CloudLab credentials configured for them to run.
To use test settings, set SET_TEST_VALUES = True in provisioner/config/settings.py

The tests are designed to be self-cleaning, but if a test fails,
you may be left with a running cluster on CloudLab. You should
manually terminate it.
"""

import datetime
import os
import subprocess
import sys
import time
import uuid

import pytest
from colorama import Fore, Style, init

import provisioner.cli
from provisioner.cli import get_state_manager
from provisioner.cloudlab_provisioner import CloudlabProvisioner
from provisioner.config.settings import DefaultSettings
from provisioner.daemon import ProvisionerDaemon
from provisioner.state_manager import CLUSTER_STATUS, StateManager

# Initialize colorama
init()


class TestLogger:
    """Custom logger for test output with color-coded messages."""

    @staticmethod
    def info(msg: str):
        print(f"{Fore.BLUE}[INFO]{Style.RESET_ALL} {msg}")

    @staticmethod
    def success(msg: str):
        print(f"{Fore.GREEN}[SUCCESS]{Style.RESET_ALL} {msg}")

    @staticmethod
    def error(msg: str):
        print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}")

    @staticmethod
    def warning(msg: str):
        print(f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL} {msg}")


logger = TestLogger()

# Test constants
TEST_USER_EMAIL = f"testuser-{uuid.uuid4()}@example.com"
TEST_USER_SSH_KEY = (
    f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB+h2XcagLMnSpOeEl/BSe5uf6rcFnPIJV16f7rdUOXj {TEST_USER_EMAIL}"
)


def run_cli_command(command_args):
    """
    Run a CLI command using subprocess and return the output and exit code.

    Args:
        command_args (list): List of command arguments to pass to the CLI

    Returns:
        tuple: (stdout, stderr, return_code) containing the command output and exit status
    """
    cmd = [sys.executable, "-m", "provisioner.cli"]
    for arg in command_args:
        if " " in arg:
            cmd.append(f'"{arg}"')
        else:
            cmd.append(arg)

    cmd_str = " ".join(cmd)
    logger.info(f"Running command: {cmd_str}")

    process = subprocess.run(cmd_str, shell=True, capture_output=True, text=True)

    return process.stdout, process.stderr, process.returncode


#
# Pytest Fixtures
#


@pytest.fixture
def state_manager():
    """
    Sets up the test environment and returns a state manager instance.

    This fixture:
    1. Cleans up any existing test database
    2. Creates a fresh state manager instance
    3. Cleans up after the test by terminating any CloudLab clusters
       and removing the test database

    Yields:
        StateManager: A fresh state manager instance for the test
    """
    if os.path.exists(DefaultSettings.DATABASE_PATH):
        os.remove(DefaultSettings.DATABASE_PATH)

    provisioner.cli._state_manager_instance = None
    sm = get_state_manager()

    yield sm

    # Cleanup after the test
    cleanup_cloudlab_clusters(sm)
    if os.path.exists(DefaultSettings.DATABASE_PATH):
        os.remove(DefaultSettings.DATABASE_PATH)
    logger.success("Test environment cleaned up")


@pytest.fixture
def daemon():
    """
    Returns a provisioner daemon instance.

    Returns:
        ProvisionerDaemon: A fresh daemon instance for the test
    """
    return ProvisionerDaemon()


def cleanup_cloudlab_clusters(state_manager: StateManager):
    """
    Cleans up any clusters created during the test.

    This function:
    1. Gets all clusters from the state manager
    2. Attempts to terminate each cluster on CloudLab
    3. Handles cases where clusters may already be deleted
    4. Logs success/failure for each operation

    Args:
        state_manager (StateManager): The state manager instance containing cluster records

    """
    logger.info("Cleaning up CloudLab clusters")
    all_clusters = state_manager.get_all_clusters()
    if not all_clusters:
        logger.info("No clusters to clean up.")
        return

    provisioner = CloudlabProvisioner()
    for cluster in all_clusters:
        slice_name = cluster["slice_name"]
        agg_name = cluster["aggregate_name"]
        if agg_name and agg_name != "<PENDING>":
            logger.info(f"Terminating cluster: {slice_name} on {agg_name}")
            try:
                provisioner.delete_experiment(slice_name, agg_name)
                logger.success(f"Successfully terminated {slice_name}.")
            except Exception as e:
                if "No slice or aggregate here" in str(e) or "No such slice here" in str(e):
                    logger.warning(f"Cluster {slice_name} already deleted.")
                else:
                    logger.error(f"Error terminating {slice_name}: {e}")
        else:
            logger.warning(f"Skipping termination for {slice_name} (no aggregate name).")


#
# Test Suite
#
# The following tests test the core functionality of the CloudLab provisioner:
# 1. test_auto_provisioning - Tests automatic cluster provisioning when lower than MIN_AVAILABLE_CLUSTERS
# 2. test_user_claim_and_relinquish - Tests user cluster claim and release workflow
# 3. test_max_clusters_per_user - Ensures users can't exceed their cluster limit
# 4. test_unclaimed_cluster_timeout - Tests automatic cleanup of unused clusters
# 5. test_max_total_clusters_limit - Tests system-wide cluster limit enforcement
# 6. test_claimed_cluster_inactivity_timeout - Tests cleanup of inactive claimed clusters
# 7. test_eval_override_for_inactivity - Tests evaluation mode claimed cluster protection
# 8. test_claimed_cluster_extension - Tests automatic claimed cluster reservation extension
# 9. test_srearena_deploy - Tests SRE Arena deployment.
#


def test_auto_provisioning(state_manager, daemon):
    """
    Tests that the provisioner creates the minimum number of available clusters on startup.

    This test checks that:
    1. Initially there are no available clusters
    2. After running checks, the minimum number of clusters are provisioned
    3. The provisioned clusters are in the correct state

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Auto Provisioning")

    assert state_manager.count_total_available_clusters() == 0

    daemon.run_all_checks()

    assert (
        state_manager.count_total_available_clusters() == DefaultSettings.MIN_AVAILABLE_CLUSTERS
    ), "Initial provisioning did not create the minimum number of available clusters"

    logger.success("Test PASSED: Auto provisioning created an available cluster.")


def test_user_claim_and_relinquish(state_manager, daemon):
    """
    Tests the complete workflow of a user claiming and relinquishing a cluster.

    This test checks that:
    1. A cluster can be auto-provisioned
    2. A user can register and claim the cluster
    3. The user can relinquish the cluster
    4. The cluster is properly terminated after relinquishment

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: User Claim and Relinquish")

    # 1. Auto-provision a cluster first
    logger.info("Provisioning a cluster for the test...")
    daemon.run_all_checks()

    assert (
        state_manager.count_total_available_clusters() == DefaultSettings.MIN_AVAILABLE_CLUSTERS
    ), "Auto-provisioning did not create the minimum number of available clusters"

    # 2. Register a user
    stdout, stderr, exit_code = run_cli_command(
        ["register", "--email", TEST_USER_EMAIL, "--ssh-key", TEST_USER_SSH_KEY]
    )
    assert exit_code == 0, f"Registration failed: {stderr}"

    # 3. User claims the cluster
    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL])
    assert exit_code == 0, f"Claim failed: {stderr}"

    # 4. Verify that a cluster was claimed
    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User did not successfully claim a cluster"
    cluster_name = claimed_clusters[0]["slice_name"]
    logger.info(f"User claimed cluster: {cluster_name}")

    # 5. User relinquishes the cluster
    stdout, stderr, exit_code = run_cli_command(
        ["relinquish", "--email", TEST_USER_EMAIL, "--experiment", cluster_name]
    )
    assert exit_code == 0, f"Relinquish failed: {stderr}"

    # 6. Check that the cluster is marked for termination
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert cluster_state["status"] == CLUSTER_STATUS.STATUS_TERMINATING, "Cluster was not marked for termination"

    # 7. Run termination check to clean it up
    daemon.run_all_checks()

    # 8. After termination, check that the cluster is marked as terminated
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert cluster_state is not None, "Cluster record was deleted"
    assert cluster_state["status"] == CLUSTER_STATUS.STATUS_TERMINATED, "Cluster was not marked as terminated"
    logger.success("Test PASSED: User successfully claimed and relinquished a cluster.")


def test_max_clusters_per_user(state_manager, daemon):
    """
    Tests that users cannot exceed their maximum allowed cluster limit.

    This test checks that:
    1. A user can claim their first cluster
    2. The user is prevented from claiming additional clusters (MAX_CLUSTERS_PER_USER = 1)
    3. The system maintains the correct cluster count

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Max Clusters Per User")

    # 1. Auto-provision a cluster first
    logger.info("Provisioning a cluster for the test...")
    daemon.run_all_checks()

    assert (
        state_manager.count_total_available_clusters() == DefaultSettings.MIN_AVAILABLE_CLUSTERS
    ), "Auto-provisioning did not create the minimum number of available clusters"

    # 2. Register a user
    stdout, stderr, exit_code = run_cli_command(
        ["register", "--email", TEST_USER_EMAIL, "--ssh-key", TEST_USER_SSH_KEY]
    )
    assert exit_code == 0, f"Registration failed: {stderr}"

    # 3. User claims cluster
    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL])
    assert exit_code == 0, f"Claim failed: {stderr}"
    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User could not claim the first cluster"
    logger.info("User claimed first cluster.")

    logger.info("Provisioning a second cluster...")
    daemon.run_all_checks()

    assert (
        state_manager.count_total_available_clusters() == DefaultSettings.MIN_AVAILABLE_CLUSTERS
    ), "Auto-provisioning did not create the minimum number of available clusters"

    # 4. User tries to claim a second cluster and should fail
    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL])

    # Verify user still only has one cluster
    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User was able to claim more than MAX_CLUSTERS_PER_USER"
    logger.success("Test PASSED: User was correctly blocked from claiming more than max clusters.")


def test_unclaimed_cluster_timeout(state_manager, daemon):
    """
    Tests that unclaimed clusters are automatically terminated after the timeout period.

    This test checks that:
    1. A cluster can be provisioned
    2. The cluster is terminated after the unclaimed timeout period
    3. The cluster state is properly updated to terminated

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Unclaimed Cluster Timeout")

    # 1. Provision a cluster
    logger.info("Provisioning a cluster for the test...")
    daemon.run_all_checks()

    assert (
        state_manager.count_total_available_clusters() == DefaultSettings.MIN_AVAILABLE_CLUSTERS
    ), "Auto-provisioning did not create the minimum number of available clusters"
    cluster_name = state_manager.get_clusters_by_status(CLUSTER_STATUS.STATUS_UNCLAIMED_READY)[0]["slice_name"]
    logger.info(f"Cluster {cluster_name} is ready and unclaimed.")

    # 2. Manipulate the created_at timestamp to be in the past
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    modified_created_at = datetime.datetime.now() - datetime.timedelta(
        hours=DefaultSettings.UNCLAIMED_CLUSTER_TIMEOUT_HOURS
    )
    state_manager.update_cluster_record(cluster_name, created_at=modified_created_at)

    # 3. Run the timeout check
    daemon.run_all_checks()

    # 4. After termination, check that the cluster is marked as terminated
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert cluster_state is not None, "Cluster record was deleted"
    assert cluster_state["status"] == CLUSTER_STATUS.STATUS_TERMINATED, "Cluster was not marked as terminated"

    logger.success("Test PASSED: Unclaimed cluster was terminated after timeout.")


def test_max_total_clusters_limit(state_manager, daemon):
    """
    Tests that the system enforces a maximum total number of clusters.

    This test checks that:
    1. Users can claim clusters up to the system-wide limit
    2. Additional claims are rejected when the limit is reached
    3. The total cluster count remains at the maximum limit

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Max Total Clusters Limit")

    # Try to claim clusters up to MAX_TOTAL_CLUSTERS + 1
    for i in range(1, DefaultSettings.MAX_TOTAL_CLUSTERS + 2):
        stdout, stderr, exit_code = run_cli_command(
            ["register", "--email", f"testuser{i}@example.com", "--ssh-key", TEST_USER_SSH_KEY]
        )
        assert exit_code == 0, f"Registration failed: {stderr}"

        if i <= DefaultSettings.MAX_TOTAL_CLUSTERS:
            stdout, stderr, exit_code = run_cli_command(["claim", "--email", f"testuser{i}@example.com"])
            assert exit_code == 0, f"Claim failed: {stderr}"
        else:
            stdout, stderr, exit_code = run_cli_command(["claim", "--email", f"testuser{i}@example.com"])
            assert "Maximum total clusters" in stdout, "Error message should mention maximum clusters limit"
            logger.success(f"Successfully tested that claim {i} was blocked due to MAX_TOTAL_CLUSTERS limit")

    # The total count should still be MAX_TOTAL_CLUSTERS
    assert (
        state_manager.count_total_managed_clusters() == DefaultSettings.MAX_TOTAL_CLUSTERS
    ), "Total cluster count should not have increased"
    logger.success("Test PASSED: Provisioner correctly enforced MAX_TOTAL_CLUSTERS limit.")


def test_claimed_cluster_inactivity_timeout(state_manager, daemon):
    """
    Tests that claimed clusters are terminated after a period of inactivity.

    This test checks that:
    1. A user can claim a cluster
    2. The cluster is terminated after the inactivity timeout period
    3. The cluster state is properly updated to terminated

    Note:
        This test is highly dependent on SSH access to the node from the
        machine running the test, and the logic for parsing auth.log. It may be flaky.

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Claimed Cluster Inactivity Timeout")

    # 1. Register a user and claim a cluster
    stdout, stderr, exit_code = run_cli_command(
        ["register", "--email", TEST_USER_EMAIL, "--ssh-key", TEST_USER_SSH_KEY]
    )
    assert exit_code == 0, f"Registration failed: {stderr}"

    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL])
    assert exit_code == 0, f"Claim failed: {stderr}"

    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User could not claim a cluster"

    cluster_name = claimed_clusters[0]["slice_name"]
    logger.info(f"Cluster {cluster_name} claimed.")

    # 2. Wait for inactivity timeout to pass
    wait_time = DefaultSettings.CLAIMED_CLUSTER_INACTIVITY_TIMEOUT_HOURS * 3600 + 15  # add 15-second buffer
    logger.info(f"Waiting for {wait_time:.1f} seconds for inactivity timeout...")
    time.sleep(wait_time)

    # 3. Run the inactivity check
    daemon.run_all_checks()

    # 4. After termination, check that the cluster is marked as terminated
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert cluster_state is not None, "Cluster record was deleted"
    assert cluster_state["status"] == CLUSTER_STATUS.STATUS_TERMINATED, "Cluster was not marked as terminated"

    logger.success("Test PASSED: Claimed inactive cluster was terminated.")


def test_eval_override_for_inactivity(state_manager, daemon):
    """
    Tests that clusters with evaluation override are protected from inactivity termination.

    This test checks that:
    1. A user can claim a cluster with evaluation override
    2. The cluster remains active after the inactivity timeout period
    3. The cluster state remains as claimed

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Eval Override for Inactivity")

    # 1. Register a user and claim a cluster with eval override
    stdout, stderr, exit_code = run_cli_command(
        ["register", "--email", TEST_USER_EMAIL, "--ssh-key", TEST_USER_SSH_KEY]
    )
    assert exit_code == 0, f"Registration failed: {stderr}"

    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL, "--eval-override"])
    assert exit_code == 0, f"Claim with eval-override failed: {stderr}"

    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User could not claim a cluster"

    cluster_name = claimed_clusters[0]["slice_name"]
    logger.info(f"Cluster {cluster_name} claimed with eval override.")

    # 2. Verify the override flag is set in the DB
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert cluster_state["evaluation_override"] in (True, 1), "Evaluation override flag not set in database"

    # 3. Wait for inactivity timeout to pass
    wait_time = DefaultSettings.CLAIMED_CLUSTER_INACTIVITY_TIMEOUT_HOURS * 3600 + 15  # add 15-second buffer
    logger.info(f"Waiting for {wait_time:.1f} seconds (inactivity timeout period)...")
    time.sleep(wait_time)

    # 4. Run the inactivity check
    daemon.run_all_checks()

    # 5. Check cluster is NOT marked for termination
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert (
        cluster_state["status"] == CLUSTER_STATUS.STATUS_CLAIMED
    ), "Cluster with eval-override was incorrectly terminated"
    logger.info("Cluster was not terminated, as expected.")
    logger.success("--eval-override correctly prevented inactivity termination.")


def test_claimed_cluster_extension(state_manager, daemon):
    """
    Tests that claimed clusters are automatically extended before expiration.

    This test checks that:
    1. A user can claim a cluster
    2. The cluster's reservation is automatically extended
    3. The extension time is properly recorded and updated

    Args:
        state_manager: Fixture providing a fresh state manager instance
        daemon: Fixture providing a fresh daemon instance
    """
    logger.info("Running Test: Claimed Cluster Extension")

    # 1. Register and claim a cluster
    stdout, stderr, exit_code = run_cli_command(
        ["register", "--email", TEST_USER_EMAIL, "--ssh-key", TEST_USER_SSH_KEY]
    )
    assert exit_code == 0, f"Registration failed: {stderr}"

    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL])
    assert exit_code == 0, f"Claim failed: {stderr}"

    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User could not claim a cluster"

    cluster_name = claimed_clusters[0]["slice_name"]
    logger.info(f"Cluster {cluster_name} claimed.")

    # 2. Record the original extension time
    original_cluster = state_manager.get_cluster_by_slice_name(cluster_name)
    assert original_cluster is not None, "Could not find claimed cluster"
    original_last_extended_at = original_cluster.get("last_extended_at")

    logger.info(f"Original last extended at: {original_last_extended_at}")

    # 3. Wait for the extension check time to pass
    wait_time = DefaultSettings.CLAIMED_CLUSTER_EXTENSION_CHECK_HOURS * 3600 + 15  # add 15-second buffer
    logger.info(f"Waiting for {wait_time:.1f} seconds for extension check...")
    time.sleep(wait_time)

    # 4. Run the extension check
    daemon.run_all_checks()

    # 5. Check if the cluster's expiry and last_extended_at have been updated
    cluster_state = state_manager.get_cluster_by_slice_name(cluster_name)
    assert cluster_state["last_extended_at"] is not None, "last_extended_at field is None"

    last_extended_at = cluster_state["last_extended_at"]
    if isinstance(last_extended_at, str):
        last_extended_at = datetime.datetime.fromisoformat(last_extended_at)

    # 6. Check if the extension time was updated
    if original_last_extended_at:
        if isinstance(original_last_extended_at, str):
            original_time = datetime.datetime.fromisoformat(original_last_extended_at)
        else:
            original_time = original_last_extended_at

    assert last_extended_at > original_time, "Cluster extension time was not updated"

    assert (datetime.datetime.now() - last_extended_at).total_seconds() < 60, "Extension time is not recent"

    logger.success("Test PASSED: Claimed cluster reservation was extended.")


def test_srearena_deploy(state_manager):
    """
    Tests that SRE Arena is deployed on a claimed cluster.
    """
    logger.info("Running Test: SRE Arena Deployment when claiming a cluster")

    # 1. Register a user and claim a cluster
    stdout, stderr, exit_code = run_cli_command(
        ["register", "--email", TEST_USER_EMAIL, "--ssh-key", TEST_USER_SSH_KEY]
    )
    assert exit_code == 0, f"Registration failed: {stderr}"

    stdout, stderr, exit_code = run_cli_command(["claim", "--email", TEST_USER_EMAIL, "--deploy-srearena"])
    assert exit_code == 0, f"Claim failed: {stderr}"

    claimed_clusters = state_manager.get_claimed_clusters_by_user(TEST_USER_EMAIL)
    assert len(claimed_clusters) == 1, "User could not claim a cluster"

    logger.success("Test PASSED: SRE Arena was deployed on a claimed cluster.")
