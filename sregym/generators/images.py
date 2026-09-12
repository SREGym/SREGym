"""Images used by fault injection and workload helpers, outside app manifests.

Published replacements use multiarch index digests. Keep these references in
the registry audit as well as the application manifests: some are pulled only
after a healthy deployment has completed.
"""

# Container references are agent-visible: use the normal application package
# and neutral release tags. Keep the behavior explicit only in internal names.
HOTEL_GEO_MISCONFIG_IMAGE = (
    "ghcr.io/sregym/hotel-reservation:20260912.1"
    "@sha256:8bc558a555f65f7522d1404ca47d70d95fc0cb58449bc039b741bd3074817838"
)
HOTEL_CORRELATED_FAULT_IMAGE = (
    "ghcr.io/sregym/hotel-reservation:20260912.2"
    "@sha256:d3c82fbcbe2fc74f47c7cb35dc72040c93c6db62084b98b560e3f69a8f130766"
)
STRESS_IMAGE = (
    "ghcr.io/sregym/stress:20260912-multiarch@sha256:60eba58b6c432c989d837e898286ff8df0d11065498b1f55090e6ed8d495dc94"
)

# Still AMD64-only. Its modified source is required for a faithful ARM rebuild;
# replacing it with an upstream healthy kube-proxy would remove the fault.
WORKLOAD_IMBALANCE_PROXY_IMAGE = "docker.io/jackcuii/kube-proxy:v1.31.12"
