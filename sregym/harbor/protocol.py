"""Names shared by the Harbor task generator, the task image and the backend.

Generated tasks embed these values in task.toml and scripts, and
docker/harbor/start.sh and sregym-ready use the same paths, so changing one
requires rebuilding the image and regenerating the dataset.
"""

# Backend listeners in the task container. The agent can reach these.
K8S_PROXY_PORT = 16443
API_PORT = 8765
# Grading listener; it requires the root-only grade token.
GRADE_PORT = 8766

# Written by the backend (root), readable by the agent: its kubeconfig and
# the setup state that sregym-ready waits on.
SHARED_DIR = "/run/sregym"
KUBECONFIG_NAME = "kubeconfig"
STATE_NAME = "state"
STATUS_NAME = "status.json"

# Root-only backend output. The logs are collected as Harbor artifacts.
BACKEND_OUTPUT_DIR = "/sregym-harbor"
GRADE_PATH = f"{BACKEND_OUTPUT_DIR}/grade.json"
# Root-only bearer token for POST /grade, generated per trial. The agent may share
# the backend's loopback, so grading must not be open to it.
GRADE_TOKEN_PATH = f"{BACKEND_OUTPUT_DIR}/grade-token"
LOG_DIR = f"{BACKEND_OUTPUT_DIR}/logs"

# Backend configuration, read from its environment (start.sh sets these from
# root-only files in the task image).
PROBLEM_ID_ENV = "SREGYM_PROBLEM_ID"
ORACLE_TOKEN_SHA256_ENV = "SREGYM_ORACLE_TOKEN_SHA256"
# Seconds the deployed application runs before the fault is injected (the
# Conductor's baseline_override_s, main.py's --baseline).
STEADY_STATE_ENV = "SREGYM_STEADY_STATE_S"

# Each task's oracle token is HMAC-SHA256(secret, task name). Tasks carry only
# the token's SHA-256; the reference solution receives the secret at run time
# through task.toml's [solution] env, which Harbor resolves for the oracle
# agent only. Published tasks therefore hold no usable token.
ORACLE_SECRET_ENV = "SREGYM_ORACLE_SECRET"
# A generated secret is kept here, at the dataset root: outside every task
# directory, so `harbor publish` never uploads it.
ORACLE_SECRET_FILE = ".sregym-oracle-secret"

# Values of the ``state`` file.
STATE_STARTING = "starting"
STATE_DEPLOYING = "deploying"
STATE_READY = "ready"
STATE_FAILED = "failed"
