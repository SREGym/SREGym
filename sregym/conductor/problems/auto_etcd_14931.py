"""Confirmed reproducible: https://github.com/etcd-io/etcd/issues/14931

Title: etcd server panic on malformed JWT token (missing claims)
Buggy: v3.5.0 through v3.5.7. Fixed: v3.5.8 (PR #15676).

Reproduction (verified 2026-04-28):
  1. Deploy etcd v3.5.8 (stock/fixed), swap to v3.5.4 (buggy)
  2. Enable JWT auth with RSA key pair
  3. Enable auth: create root user/role, etcdctl auth enable
  4. Craft JWT token signed with correct private key but MISSING
     username and revision claims (only has exp)
  5. Send any gRPC/HTTP request with the malformed token
  6. etcd panics: "interface conversion: interface {} is nil, not string"
     at server/auth/jwt.go:77

Root cause: tokenJWT.info() performs unsafe type assertions on claims
without checking for nil: `claims["username"].(string)` panics when
the key is absent in the map (map access returns nil interface{}).
"""

import json as _json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_RSA_KEY_BITS = 2048


def _generate_rsa_keypair():
    """Generate RSA key pair and return (private_pem, public_pem) as strings."""
    result = subprocess.run(
        f"openssl genrsa {_RSA_KEY_BITS}",
        shell=True, capture_output=True, text=True,
    )
    private_pem = result.stdout
    result = subprocess.run(
        "openssl rsa -pubout",
        shell=True, input=private_pem, capture_output=True, text=True,
    )
    public_pem = result.stdout
    return private_pem, public_pem


class AutoEtcd14931(GenericCustomBuildProblem):
    db_name = "etcd"
    db_version = "3.5.8"
    source_git_ref = "v3.5.4"
    build_image = "golang:1.19"
    root_cause_description = (
        "etcd server panics when receiving a JWT token that is validly "
        "signed but missing the username and/or revision claims. The "
        "tokenJWT.info() method in server/auth/jwt.go performs unsafe "
        "type assertions (claims[\"username\"].(string) and "
        "claims[\"revision\"].(float64)) without checking for nil. When "
        "auth is enabled with --auth-token=jwt, any request with a "
        "properly-signed token missing these claims crashes the server "
        "with 'interface conversion: interface {} is nil, not string'."
    )
    continuous_reproducer = True

    @mark_fault_injected
    def inject_fault(self):
        """Enable JWT auth, swap to buggy image, send malicious token."""
        ns = self.app.namespace
        sts_name = self.app.cluster_name

        private_pem, public_pem = _generate_rsa_keypair()
        logger.info("[etcd#14931] Generated RSA key pair for JWT auth")

        with tempfile.TemporaryDirectory() as tmpdir:
            priv_path = Path(tmpdir) / "private.pem"
            pub_path = Path(tmpdir) / "public.pem"
            priv_path.write_text(private_pem)
            pub_path.write_text(public_pem)

            subprocess.run(
                f"kubectl create secret generic etcd-jwt-keys -n {ns} "
                f"--from-file=private.pem={priv_path} "
                f"--from-file=public.pem={pub_path}",
                shell=True, check=True, capture_output=True, text=True,
            )

        logger.info("[etcd#14931] Patching StatefulSet for JWT auth")
        patch = _json.dumps({
            "spec": {"template": {"spec": {
                "shareProcessNamespace": True,
                "containers": [{
                    "name": "etcd",
                    "env": [{
                        "name": "ETCD_AUTH_TOKEN",
                        "value": "jwt,pub-key=/keys/public.pem,priv-key=/keys/private.pem,sign-method=RS256,ttl=10m",
                    }],
                    "volumeMounts": [{
                        "name": "jwt-keys",
                        "mountPath": "/keys",
                        "readOnly": True,
                    }],
                }],
                "volumes": [{
                    "name": "jwt-keys",
                    "secret": {"secretName": "etcd-jwt-keys"},
                }],
            }}}
        })
        subprocess.run(
            f"kubectl patch statefulset {sts_name} -n {ns} --type=strategic -p '{patch}'",
            shell=True, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            f"kubectl delete pod -n {ns} -l app.kubernetes.io/instance={sts_name} "
            f"--force --grace-period=0",
            shell=True, capture_output=True, text=True,
        )
        logger.info("[etcd#14931] Waiting for pod restart with JWT auth")
        time.sleep(5)
        self.app._wait_for_cluster_ready(timeout=300)

        logger.info(f"[etcd#14931] Swapping to buggy image: {self._custom_image}")
        self.app.inject_buggy_image(self._custom_image)

        logger.info("[etcd#14931] Enabling auth (root user + role)")
        self.app.run_reproducer(
            "#!/bin/sh\n"
            "etcdctl user add root --new-user-password=root 2>&1\n"
            "etcdctl role add root 2>&1\n"
            "etcdctl user grant-role root root 2>&1\n"
            "etcdctl auth enable 2>&1\n"
        )

        logger.info("[etcd#14931] Crafting bad JWT and sending to crash etcd")
        bad_jwt_script = (
            "#!/bin/sh\n"
            "HEADER=$(echo -n '{\"alg\":\"RS256\",\"typ\":\"JWT\"}' | base64 -w0 | tr '+/' '-_' | tr -d '=')\n"
            "NOW=$(date +%s)\n"
            "EXP=$((NOW + 3600))\n"
            'PAYLOAD=$(echo -n "{\\\"exp\\\":$EXP}" | base64 -w0 | tr "+/" "-_" | tr -d "=")\n'
            'SIGNATURE=$(echo -n "${HEADER}.${PAYLOAD}" | openssl dgst -sha256 -sign /keys/private.pem | base64 -w0 | tr "+/" "-_" | tr -d "=")\n'
            'TOKEN="${HEADER}.${PAYLOAD}.${SIGNATURE}"\n'
            'KEY=$(echo -n "test" | base64)\n'
            'curl -s -H "Authorization: $TOKEN" http://127.0.0.1:2379/v3/kv/range -d "{\\\"key\\\":\\\"$KEY\\\"}" 2>&1\n'
        )
        self.app.run_reproducer(bad_jwt_script)

        logger.info("[etcd#14931] Waiting for CrashLoopBackOff")
        self._wait_for_any_crash_loop(timeout=300)
        logger.info("[etcd#14931] CrashLoopBackOff confirmed — fault injected")

        cont_script = (
            "#!/bin/sh\n"
            "HEADER=$(echo -n '{\"alg\":\"RS256\",\"typ\":\"JWT\"}' | base64 -w0 | tr '+/' '-_' | tr -d '=')\n"
            "NOW=$(date +%s)\n"
            "EXP=$((NOW + 3600))\n"
            'PAYLOAD=$(echo -n "{\\\"exp\\\":$EXP}" | base64 -w0 | tr "+/" "-_" | tr -d "=")\n'
            'SIGNATURE=$(echo -n "${HEADER}.${PAYLOAD}" | openssl dgst -sha256 -sign /keys/private.pem | base64 -w0 | tr "+/" "-_" | tr -d "=")\n'
            'TOKEN="${HEADER}.${PAYLOAD}.${SIGNATURE}"\n'
            'KEY=$(echo -n "test" | base64)\n'
            'curl -s -H "Authorization: $TOKEN" http://${ETCDCTL_ENDPOINTS#http://}/v3/kv/range -d "{\\\"key\\\":\\\"$KEY\\\"}" 2>&1 || true\n'
            "sleep 3\n"
            "etcdctl endpoint health 2>&1\n"
        )
        self.app.deploy_continuous_reproducer(cont_script, None)

    @mark_fault_injected
    def recover_fault(self):
        """Restore stock (fixed) image — v3.5.8 handles bad JWT gracefully."""
        logger.info("[etcd#14931] Restoring stock (fixed) image")
        self.app.restore_stock_image(custom_image=self._custom_image)

    def _wait_for_any_crash_loop(self, timeout: int = 300):
        """Wait until any pod in the namespace enters CrashLoopBackOff."""
        ns = self.app.namespace
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = subprocess.run(
                f"kubectl get pods -n {ns} -o json",
                shell=True, capture_output=True, text=True,
            )
            try:
                data = _json.loads(out.stdout)
                for pod in data.get("items", []):
                    for cs in pod.get("status", {}).get("containerStatuses", []):
                        reason = (
                            cs.get("state", {}).get("waiting", {}).get("reason", "")
                        )
                        exit_code = (
                            cs.get("state", {}).get("terminated", {}).get("exitCode")
                        )
                        restarts = cs.get("restartCount", 0)
                        if reason in ("CrashLoopBackOff", "Error") or (
                            exit_code is not None and exit_code != 0 and restarts >= 2
                        ):
                            pod_name = pod["metadata"]["name"]
                            logger.info(
                                f"Crash confirmed on '{pod_name}': "
                                f"reason={reason or 'exit'} restarts={restarts}"
                            )
                            return
            except Exception:
                pass
            time.sleep(10)
        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for CrashLoopBackOff in '{ns}'"
        )
