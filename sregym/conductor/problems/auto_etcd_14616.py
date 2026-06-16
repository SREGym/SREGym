"""Auto-generated from https://github.com/etcd-io/etcd/issues/14616

Title: Leases are not revoked when JWT authentication enabled
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd14616(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/14616"
    root_cause_description = (
        "Leases are not revoked when JWT authentication enabled. ### What happened? When ETCD is configured to run with JWT authentication specifying public key only, the keys under leases stay alive indefinitely. When auth token is configured with `--auth-token=jwt,pub-key=jwt_cert.pem,sign-method=ES256,ttl=5m` with no `priv-key` specified, the keys created under lease are not revoked after their TTL is expired. ### What did you expect to happen? Temporary keys are deleted automatically when their TTL expired. ### How can we reproduce it (as minimally and precisely as possible)? The auth token is configured with `--auth-token=jwt,pub-key=jwt_cert.pem,sign-method=ES256,ttl=5m` * Run etcd with `--auth-token=jwt,pub-key=jwt_cert.pem,sign-method=ES256,ttl=5m` argument * ``` # Enable auth etcdctl auth enable # Create lease etcdctl lease"
    )
