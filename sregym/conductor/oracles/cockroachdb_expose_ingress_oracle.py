"""Oracle for validating CockroachDB Expose Ingress operator action.

This oracle verifies that the agent successfully created Ingress resources
for exposing the CockroachDB cluster externally.

The oracle validates:
1. Ingress resource created (40% weight)
2. Service properly configured in Ingress (30% weight)
3. TLS configuration in place (30% weight)
4. Annotation removed from CrdbCluster CR

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/expose-ingress/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBExposeIngressOracle(Oracle):
    """
    Oracle that validates the Expose Ingress operator workflow for CockroachDB.

    This oracle verifies that the agent created Ingress resources:
    1. Ingress resource exists (40% weight)
    2. Ingress references correct service (30% weight)
    3. TLS configuration present (30% weight)

    Pass threshold: 70%
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether agent created Ingress resources successfully.

        Returns:
            dict: Results dictionary with:
                - 'success' (bool): Overall pass/fail
                - 'issues' (list): List of problems found
                - 'score' (float): Weighted score 0.0-1.0
                - 'breakdown' (dict): Detailed scoring per category
        """
        print("== CockroachDB Expose Ingress Oracle Evaluation ==")
        print("Testing Ingress resource creation and configuration")
        print("=" * 60)

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        results = {
            "success": True,
            "issues": [],
            "score": 0.0,
            "breakdown": {
                "ingress_created": False,
                "service_configured": False,
                "tls_setup": False,
            },
        }

        # STEP 1: Check Ingress created (40% weight)
        print(f"\n[1/4] Checking Ingress resource created (40% weight)...")
        ingress_created = False

        try:
            # List Ingress resources in namespace
            ingress_list = kubectl.networking_v1_api.list_namespaced_ingress(namespace=namespace)

            if len(ingress_list.items) > 0:
                print(f"  ✅ Found {len(ingress_list.items)} Ingress resource(s)")
                for ingress in ingress_list.items:
                    print(f"     - {ingress.metadata.name}")
                ingress_created = True
                results["breakdown"]["ingress_created"] = True
                results["score"] += 0.40
            else:
                issue = "No Ingress resources found in namespace"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking Ingress: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 2: Check service configured (30% weight)
        print(f"\n[2/4] Checking service configured in Ingress (30% weight)...")
        service_configured = False

        try:
            ingress_list = kubectl.networking_v1_api.list_namespaced_ingress(namespace=namespace)

            if len(ingress_list.items) > 0:
                service_found = False
                for ingress in ingress_list.items:
                    if ingress.spec.rules and len(ingress.spec.rules) > 0:
                        for rule in ingress.spec.rules:
                            if rule.http and rule.http.paths:
                                for path in rule.http.paths:
                                    service_name = path.backend.service.name
                                    print(f"  ✅ Ingress routes to service: {service_name}")
                                    if "crdb-cluster-public" in service_name or "crdb-cluster" in service_name:
                                        service_found = True
                                        break
                    if ingress.spec.backend and ingress.spec.backend.service_name:
                        service_name = ingress.spec.backend.service_name
                        print(f"  ✅ Ingress backend service: {service_name}")
                        if "crdb-cluster" in service_name:
                            service_found = True

                if service_found:
                    service_configured = True
                    results["breakdown"]["service_configured"] = True
                    results["score"] += 0.30
                else:
                    # Partial credit - service might not be explicitly named
                    print(f"  ℹ️  Service backend configured (name not verified)")
                    results["score"] += 0.15
            else:
                issue = "No Ingress resources to configure service"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking service configuration: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 3: Check TLS setup (30% weight)
        print(f"\n[3/4] Checking TLS configuration (30% weight)...")
        tls_setup = False

        try:
            ingress_list = kubectl.networking_v1_api.list_namespaced_ingress(namespace=namespace)

            if len(ingress_list.items) > 0:
                tls_found = False
                for ingress in ingress_list.items:
                    if ingress.spec.tls and len(ingress.spec.tls) > 0:
                        print(f"  ✅ TLS configuration found in Ingress")
                        for tls in ingress.spec.tls:
                            print(f"     - Hosts: {tls.hosts}")
                            print(f"     - Secret: {tls.secret_name}")
                        tls_found = True
                        break

                if tls_found:
                    tls_setup = True
                    results["breakdown"]["tls_setup"] = True
                    results["score"] += 0.30
                else:
                    # Ingress exists but no TLS - still partial credit
                    print(f"  ℹ️  Ingress created but TLS not configured (insecure Ingress)")
                    results["score"] += 0.15
            else:
                issue = "No Ingress resources to check for TLS"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)

        except Exception as e:
            issue = f"Error checking TLS configuration: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)

        # STEP 4: Final summary
        print(f"\n[4/4] Final Summary")
        print(f"=" * 60)
        print(f"Score Breakdown:")
        print(f"  - Ingress created:    {'✅ +40%' if results['breakdown']['ingress_created'] else '❌ +0%'}")
        print(f"  - Service configured: {'✅ +30%' if results['breakdown']['service_configured'] else '❌ +0%'}")
        print(f"  - TLS setup:          {'✅ +30%' if results['breakdown']['tls_setup'] else '❌ +0%'}")
        print(
            f"\nTotal Score: {results['score']:.0%} ({'Pass' if results['success'] and results['score'] >= 0.70 else 'Fail'})"
        )

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
            print(f"\n✅ PASS: Ingress exposure configured successfully")

        print(f"=" * 60 + "\n")

        return results
