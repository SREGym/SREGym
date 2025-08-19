import os

from dotenv import load_dotenv
from geni.aggregate.cloudlab import Clemson, Utah, Wisconsin

load_dotenv(override=True)

SET_TEST_VALUES = False


# Default settings
class DefaultSettings:
    #### Default Settings ####
    DEFAULT_HARDWARE_TYPE = "c220g5" if not SET_TEST_VALUES else "m510"
    DEFAULT_OS_TYPE = "UBUNTU22-64-STD"
    DEFAULT_NODE_COUNT = 3 if not SET_TEST_VALUES else 1
    DEFAULT_DURATION_HOURS = 16 if not SET_TEST_VALUES else 0.05
    DEFAULT_DESCRIPTION = "Cloudlab Experiment"

    MIN_AVAILABLE_CLUSTERS = 2 if not SET_TEST_VALUES else 1
    MAX_TOTAL_CLUSTERS = 8 if not SET_TEST_VALUES else 2
    MAX_CLUSTERS_PER_USER = 2 if not SET_TEST_VALUES else 1
    UNCLAIMED_CLUSTER_TIMEOUT_HOURS = 16 if not SET_TEST_VALUES else 1
    CLAIMED_CLUSTER_DEFAULT_DURATION_HOURS = 100 if not SET_TEST_VALUES else 0.1
    CLAIMED_CLUSTER_INACTIVITY_TIMEOUT_HOURS = 48 if not SET_TEST_VALUES else 0.05
    CLAIMED_CLUSTER_EXTENSION_CHECK_HOURS = 24 if not SET_TEST_VALUES else 0.025

    DATABASE_PATH = "database.sqlite3"

    DEFAULT_SSH_TIME_OUT_SECONDS = 30  # 30

    LOG_PATH = "logs/"

    #### Provisioner Credentials ####
    PROVISIONER_DEFAULT_SSH_USERNAME = "srearena"
    PROVISIONER_SSH_PRIVATE_KEY_PATH = os.getenv("PROVISIONER_SSH_PRIVATE_KEY_PATH")

    #### Daemon Settings ####
    SCHEDULER_INTERVAL_MINUTES = 5

    #### SRE Arena Settings ####
    DEFAULT_POD_NETWORK_CIDR = "192.168.0.0/16"
    DEPLOY_KEY_PATH = os.getenv("DEPLOY_KEY_PATH")


CLOUD_LAB_CONTEXT_JSON = {
    "framework": "emulab-ch2",
    "cert-path": os.getenv("CLOUDLAB_CERT_PATH"),
    "key-path": os.getenv("CLOUDLAB_KEY_PATH"),
    "user-name": "srearena",
    "user-urn": "urn:publicid:IDN+emulab.net+user+srearena",
    "user-pubkeypath": os.getenv("PROVISIONER_SSH_PUBLIC_KEY_PATH"),
    "project": os.getenv("CLOUD_PROJECT_NAME"),
}

# Aggregates mapping
AGGREGATES_MAP = {
    "clemson": Clemson,
    "utah": Utah,
    "wisconsin": Wisconsin,
    "cloudlab clemson": Clemson,
    "cloudlab utah": Utah,
    "cloudlab wisconsin": Wisconsin,
    "cl-clemson": Clemson,
    "cl-wisconsin": Wisconsin,
    "cl-utah": Utah,
}

# Hardware types
PRIORITY_HARDWARE_TYPES = ["c220g5", "c220g4", "c220g3", "c220g2", "c220g1"]

# OS types
OS_TYPES = [
    "UBUNTU22-64-STD",
    "UBUNTU20-64-STD",
    "UBUNTU18-64-STD",
    "UBUNTU16-64-STD",
    "DEBIAN11-64-STD",
    "DEBIAN10-64-STD",
    "FEDORA36-64-STD",
    "CENTOS7-64-STD",
    "CENTOS8-64-STD",
    "RHEL8-64-STD",
]

# The first error means deletion not successful have to retry
# The second error means experiment does not exist maybe already deleted and no need to retry
DELETE_EXPERIMENT_ERRORS = [
    "resource is busy; try again later",  # -> retry
    "No such slice here",  # -> no need to retry,
    "get_credentials encountered an error requesting the slice credential: No such Slice",  # -> no need to retry, already expired
    "expired on",  # -> no need to retry, already expired
]
