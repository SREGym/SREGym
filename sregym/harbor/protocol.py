"""Names shared by the Harbor task generator and the SREGym sidecar backend.

Generated tasks embed these values in Compose files, task.toml and scripts, so
changing one requires regenerating the dataset.
"""

# Compose service that runs the backend. Agents reach it by this DNS name.
SERVICE_NAME = "sregym"

# Public listeners on the sidecar, reachable from the agent container.
K8S_PROXY_PORT = 16443
API_PORT = 8765
# Loopback-only listener used by Harbor's collect hook after the agent stops.
GRADE_PORT = 8766

# Named volume shared by both services: written by the backend, mounted
# read-only in the agent container.
BACKEND_SHARED_DIR = "/run/sregym-harbor/shared"
AGENT_SHARED_DIR = "/run/sregym"
KUBECONFIG_NAME = "kubeconfig"
STATE_NAME = "state"
STATUS_NAME = "status.json"

# Sidecar paths collected as Harbor artifacts. The grade is re-materialized at
# the same path inside the separate verifier container.
BACKEND_OUTPUT_DIR = "/sregym-harbor"
GRADE_PATH = f"{BACKEND_OUTPUT_DIR}/grade.json"
LOG_DIR = f"{BACKEND_OUTPUT_DIR}/logs"

# Backend configuration read from the sidecar environment.
PROBLEM_ID_ENV = "SREGYM_PROBLEM_ID"
ORACLE_TOKEN_SHA256_ENV = "SREGYM_ORACLE_TOKEN_SHA256"

# Values of the ``state`` file.
STATE_STARTING = "starting"
STATE_DEPLOYING = "deploying"
STATE_READY = "ready"
STATE_FAILED = "failed"
