"""Start the upstream sidecar with response-aware session dependencies."""

import loadgen_sidecar
from loadgen.runner import DRIVERS
from loadgen.session import _MAX_OWNED_ROOTS
from session_dependencies import register

if __name__ == "__main__":
    register(DRIVERS, _MAX_OWNED_ROOTS)
    loadgen_sidecar.main()
