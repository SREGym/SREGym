"""Oracle for validating CockroachDB Generate Cert operator action.

This oracle verifies that the agent successfully generated TLS certificates
for a CockroachDB cluster deployment.

The oracle validates:
1. CA certificate generated
2. Node certificates generated and valid
3. Client certificate generated

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/generate-cert/
"""

from sregym.conductor.oracles.base import Oracle


class CockroachDBGenerateCertOracle(Oracle):
    """
    Oracle that validates certificate generation before deployment.

    Weighted scoring breakdown:
    - ca_cert: 30% - CA certificate created
    - node_cert: 40% - Node certificates created
    - client_cert: 30% - Client certificate created

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent successfully generated all certificates.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Generate Cert Oracle Evaluation ==")
        print("Testing certificate generation")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {"ca_cert": False, "node_cert": False, "client_cert": False},
        }

        # STEP 1: Check if CA certificate was created (30% weight)
        print(f"\n[1/3] Checking CA certificate (30% weight)...")
        ca_cert_exists = False

        try:
            secret_cmd = (
                f"kubectl -n {namespace} get secret crdb-tls-certs -o jsonpath='{{.data.ca\\.crt}}' 2>/dev/null"
            )
            ca_output = kubectl.exec_command(secret_cmd)

            if ca_output and len(ca_output.strip()) > 0:
                print(f"  ✅ CA certificate found in TLS secret")
                ca_cert_exists = True
                results["breakdown"]["ca_cert"] = True
                results["score"] += 0.30
            else:
                issue = f"CA certificate not found in secret"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not verify CA certificate: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check if node certificates were created (40% weight)
        print(f"\n[2/3] Checking node certificates (40% weight)...")
        node_certs_exist = False

        try:
            # Check for node certificate(s) in secret
            node_cert_cmd = (
                f"kubectl -n {namespace} get secret crdb-tls-certs -o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null"
            )
            node_cert_output = kubectl.exec_command(node_cert_cmd)

            if node_cert_output and len(node_cert_output.strip()) > 0:
                print(f"  ✅ Node certificate(s) found in TLS secret")
                node_certs_exist = True
                results["breakdown"]["node_cert"] = True
                results["score"] += 0.40
            else:
                # Try alternative: check if secret has any cert data
                try:
                    secret_data_cmd = (
                        f"kubectl -n {namespace} get secret crdb-tls-certs -o jsonpath='{{.data}}' 2>/dev/null"
                    )
                    secret_data = kubectl.exec_command(secret_data_cmd)
                    if "crt" in secret_data or "cert" in secret_data:
                        print(f"  ✅ Node certificate data found in secret")
                        node_certs_exist = True
                        results["breakdown"]["node_cert"] = True
                        results["score"] += 0.40
                    else:
                        issue = f"Node certificate not found in secret"
                        print(f"  ❌ {issue}")
                        results["issues"].append(issue)
                except Exception:
                    issue = f"Node certificate not found"
                    print(f"  ❌ {issue}")
                    results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not verify node certificates: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check if client certificate was created (30% weight)
        print(f"\n[3/3] Checking client certificate (30% weight)...")
        client_cert_exists = False

        try:
            # Check for client certificate or key in secret
            client_cert_cmd = (
                f"kubectl -n {namespace} get secret crdb-tls-certs -o jsonpath='{{.data.tls\\.key}}' 2>/dev/null"
            )
            client_cert_output = kubectl.exec_command(client_cert_cmd)

            if client_cert_output and len(client_cert_output.strip()) > 0:
                print(f"  ✅ Client certificate/key found in TLS secret")
                client_cert_exists = True
                results["breakdown"]["client_cert"] = True
                results["score"] += 0.30
            else:
                issue = f"Client certificate/key not found in secret"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Could not verify client certificate: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # Final summary
        print(f"\n[Summary] Final Evaluation")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - CA certificate:      {'✅ +30%' if results['breakdown']['ca_cert'] else '❌ +0%'}")
        print(f"  - Node certificates:   {'✅ +40%' if results['breakdown']['node_cert'] else '❌ +0%'}")
        print(f"  - Client certificate:  {'✅ +30%' if results['breakdown']['client_cert'] else '❌ +0%'}")
        print(f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['score'] >= 0.70 else 'Fail'})")

        if results["issues"]:
            print(f"\nIssues found ({len(results['issues'])}):")
            for i, issue in enumerate(results["issues"], 1):
                print(f"  {i}. {issue}")

        # Overall pass/fail
        if results["score"] < 0.70:
            results["success"] = False
            print(f"\n❌ FAIL: Score {results['score']:.0%} below 70% threshold")
        else:
            results["success"] = True
            print(f"\n✅ PASS: Certificate generation completed successfully")

        print(f"=" * 60 + "\n")

        return results
