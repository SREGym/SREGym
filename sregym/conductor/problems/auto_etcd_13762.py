"""Auto-generated from https://github.com/etcd-io/etcd/issues/13762

Title: grpc health check crashes etcd
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd13762(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/13762"
    root_cause_description = (
        "grpc health check crashes etcd. ### What happened? When a grpc healthcheck is configured for /health, etcd panics with \"not implemented\" ### What did you expect to happen? etcd handles the health check successfully. ### How can we reproduce it (as minimally and precisely as possible)? Use https://github.com/grpc-ecosystem/grpc-health-probe to do a tls grpc health check on a 3.4.10 server. ### Anything else we need to know? verified 3.5.1 works fine. ### Etcd version (please run commands below) <details> ```console $ etcd --version WARNING: Package \"github.com/golang/protobuf/protoc-gen-go/generator\" is deprecated. A future release of golang/protobuf will delete this package, which has long been excluded from the compatibility promise. etcd Version: 3.4.10 Git SHA: Not provided (us"
    )
