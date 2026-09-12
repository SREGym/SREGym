"""Images used by fault injection and workload helpers, outside app manifests.

Published replacements use multiarch index digests. Keep these references in
the registry audit as well as the application manifests: some are pulled only
after a healthy deployment has completed.
"""

HOTEL_GEO_MISCONFIG_IMAGE = (
    "ghcr.io/sregym/hotel-geo-misconfig:20260912-multiarch"
    "@sha256:7a4b1eca66e124ca9abe452e512f005a646869d33ae0a493c735259d59694499"
)
HOTEL_CORRELATED_FAULT_IMAGE = (
    "ghcr.io/sregym/hotel-correlated-fault:20260912-multiarch"
    "@sha256:082c9ffa9764f8c82cce7ac3f00f54af351575fc1d38cf0e8672da1b63b86220"
)
STRESS_IMAGE = (
    "ghcr.io/sregym/stress:20260912-multiarch@sha256:60eba58b6c432c989d837e898286ff8df0d11065498b1f55090e6ed8d495dc94"
)

# Still AMD64-only. Its modified source is required for a faithful ARM rebuild;
# replacing it with an upstream healthy kube-proxy would remove the fault.
WORKLOAD_IMBALANCE_PROXY_IMAGE = "docker.io/jackcuii/kube-proxy:v1.31.12"
