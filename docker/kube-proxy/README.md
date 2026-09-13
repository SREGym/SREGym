# Kubernetes proxy release 1

This is the proxy used by the `workload_imbalance` benchmark, not a healthy
cluster default. Keep its published repository and release tag neutral.

The original AMD64 image is preserved by digest. Its binary identifies Go
1.23.11 and Kubernetes `v1.31.12-2+02f6f8841781aa`, with uncommitted changes.
That source revision is absent from the author's public fork.

The ARM build uses the pinned Kubernetes v1.31.12 source and toolchain. The
included patch reproduces the original iptables probability calculation:
`fmt.Sprintf("%0.10f", 0.0114514)` instead of `1 / endpointCount`.
Consequently, every non-final endpoint receives a 1.14514% conditional match
probability; most traffic reaches the last endpoint. This value was recovered
from the original binary's probability-table initializer, including the float64
constant `0x3f8773d4e3f28340` and `%0.10f` format string.

This is a behavior-preserving reconstruction, not a claim that the unpublished
source or entire binary has been recovered. Validate actual generated iptables
rules against the original image before publishing; a version check alone does
not establish preservation of the benchmark fault.

`bash docker/test_image.sh kube-proxy-1 IMAGE arm64` checks the version and runs
the real proxy against a credential-free fixture API. It asserts all four
conditional probabilities and five endpoint rules. The test needs Docker and
Python 3; `NET_ADMIN` is granted only inside a disposable container, with no host
networking, Kubernetes credentials, or changes to any real cluster. Use `amd64`
to check the other platform or compare with the original image.
