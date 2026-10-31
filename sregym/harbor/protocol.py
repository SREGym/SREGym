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
# Seconds the deployed application runs before the fault is injected (the
# Conductor's baseline_override_s, main.py's --baseline).
STEADY_STATE_ENV = "SREGYM_STEADY_STATE_S"

# The unprivileged user Harbor runs agents as (task.toml [agent] user).
AGENT_USER = "agent"
# Harbor's oracle agent copies the task's solution/ here before running
# solve.sh; no other agent gets it. POST /oracle/recover requires it.
ORACLE_SOLUTION_PATH = "/solution/solve.sh"

# Values of the ``state`` file.
STATE_STARTING = "starting"
STATE_DEPLOYING = "deploying"
STATE_CHECKING_FAULT = "checking_fault"
STATE_READY = "ready"
STATE_FAILED = "failed"
