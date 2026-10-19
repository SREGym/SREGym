"""Start the upstream sidecar with response-aware session dependencies."""

import loadgen_sidecar
from loadgen.runner import DRIVERS
from session_dependencies import register

if __name__ == "__main__":
    register(DRIVERS)
    loadgen_sidecar.main()
