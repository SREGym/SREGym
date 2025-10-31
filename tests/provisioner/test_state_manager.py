import os
from pathlib import Path

from provisioner.state_manager import CLUSTER_STATUS, StateManager
from provisioner.utils.logger import logger

if __name__ == "__main__":
    logger.info("StateManager direct execution test started.")
    DB_FILE = "test_provisioner_state.sqlite3"
    db_path_obj = Path(DB_FILE)
    if db_path_obj.exists():
        os.remove(DB_FILE)

    sm = StateManager(DB_FILE)
    sm.add_user("user1", "ssh-rsa AAA...")

    sample_login_info = [
        ["control", "sregym", "c220g5-111111.wisc.cloudlab.us", 22],
        ["compute1", "sregym", "c220g5-222222.wisc.cloudlab.us", 22],
    ]

    slice1 = sm.create_cluster_record(
        slice_name="testslice001",
        aggregate_name="utah",
        hardware_type="m510",
        os_type="UBUNTU20-64-STD",
        node_count=1,
        login_info=sample_login_info,
        status=CLUSTER_STATUS.STATUS_UNCLAIMED_READY,
    )
    assert slice1 == "testslice001"

    cluster1_data = sm.get_cluster_by_slice_name("testslice001")
    logger.info(f"Cluster1 Data: {cluster1_data}")
    assert cluster1_data and cluster1_data["status"] == CLUSTER_STATUS.STATUS_UNCLAIMED_READY
    assert cluster1_data["login_info"] == sample_login_info

    # Test update with login_info
    new_login_info = [["control", "NewUser", "new.host.name", 22]]
    sm.update_cluster_record("testslice001", login_info=new_login_info, status=CLUSTER_STATUS.STATUS_CLAIMED)
    cluster1_updated = sm.get_cluster_by_slice_name("testslice001")
    logger.info(f"Cluster1 Updated Data: {cluster1_updated}")
    assert cluster1_updated["login_info"] == new_login_info
    assert cluster1_updated["status"] == CLUSTER_STATUS.STATUS_CLAIMED

    logger.info("StateManager direct execution test finished successfully.")
