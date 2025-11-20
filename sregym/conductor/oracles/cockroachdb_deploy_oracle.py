"""Oracle for validating CockroachDB Deploy operator action.

This oracle verifies that the agent successfully created all core Kubernetes resources
needed for a CockroachDB cluster, simulating the operator's Deploy reconciler.

Reference: kubernetes-agent-benchmark/cockroachdb/replacing-operator/deploy/
"""

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class CockroachDBDeployOracle(Oracle):
    """
    Oracle that validates successful deployment of CockroachDB cluster resources.

    This oracle verifies that the agent created:
    1. Discovery Service (headless) for internal cluster communication
    2. Public Service for client connections
    3. StatefulSet with 3 CockroachDB pods
    4. PodDisruptionBudget for high availability

    All resources should match the configuration from the CrdbCluster CR.
    """

    importance = 1.0

    def evaluate(self) -> dict:
        """
        Evaluate whether all CockroachDB cluster resources have been correctly deployed.

        Returns:
            dict: Results dictionary with 'success' (bool) and 'issues' (list) keys
        """
        print("== CockroachDB Deploy Oracle Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cluster_name = self.problem.cluster_name
        results = {"success": True, "issues": []}

        expected_replicas = 3
        expected_grpc_port = 26257
        expected_http_port = 8080

        # Step 1: Check Discovery Service (headless)
        print(f"\n[1/4] Checking Discovery Service (headless)...")
        try:
            discovery_svc = kubectl.core_v1_api.read_namespaced_service(name=cluster_name, namespace=namespace)

            # Verify it's headless (clusterIP: None)
            if discovery_svc.spec.cluster_ip != "None":
                issue = f"Discovery service should be headless (clusterIP: None), got: {discovery_svc.spec.cluster_ip}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Discovery service is headless")

            # Verify ports
            ports_ok = False
            grpc_found = http_found = False
            for port in discovery_svc.spec.ports:
                if port.name == "grpc" and port.port == expected_grpc_port:
                    grpc_found = True
                if port.name == "http" and port.port == expected_http_port:
                    http_found = True

            if grpc_found and http_found:
                print(
                    f"  ✅ Discovery service has correct ports (grpc={expected_grpc_port}, http={expected_http_port})"
                )
            else:
                issue = f"Discovery service missing ports: grpc={grpc_found}, http={http_found}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False

        except ApiException as e:
            if e.status == 404:
                issue = f"Discovery service '{cluster_name}' not found"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                issue = f"Error checking discovery service: {e.reason}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
        except Exception as e:
            issue = f"Error checking discovery service: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 2: Check Public Service
        print(f"\n[2/4] Checking Public Service...")
        try:
            public_svc_name = f"{cluster_name}-public"
            public_svc = kubectl.core_v1_api.read_namespaced_service(name=public_svc_name, namespace=namespace)

            # Verify it's ClusterIP (not None)
            if public_svc.spec.cluster_ip == "None":
                issue = "Public service should NOT be headless"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ Public service has ClusterIP: {public_svc.spec.cluster_ip}")

            # Verify ports
            grpc_found = http_found = False
            for port in public_svc.spec.ports:
                if port.name == "grpc" and port.port == expected_grpc_port:
                    grpc_found = True
                if port.name == "http" and port.port == expected_http_port:
                    http_found = True

            if grpc_found and http_found:
                print(f"  ✅ Public service has correct ports")
            else:
                issue = f"Public service missing ports: grpc={grpc_found}, http={http_found}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False

        except ApiException as e:
            if e.status == 404:
                issue = f"Public service '{public_svc_name}' not found"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                issue = f"Error checking public service: {e.reason}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
        except Exception as e:
            issue = f"Error checking public service: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 3: Check StatefulSet
        print(f"\n[3/4] Checking StatefulSet...")
        try:
            sts = kubectl.apps_v1_api.read_namespaced_stateful_set(name=cluster_name, namespace=namespace)

            # Verify replicas
            if sts.spec.replicas != expected_replicas:
                issue = f"StatefulSet replicas should be {expected_replicas}, got {sts.spec.replicas}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ StatefulSet has {expected_replicas} replicas")

            # Verify serviceName points to discovery service
            if sts.spec.service_name != cluster_name:
                issue = f"StatefulSet serviceName should be '{cluster_name}', got '{sts.spec.service_name}'"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ StatefulSet serviceName points to discovery service")

            # Verify ServiceAccount
            expected_sa = f"{cluster_name}-sa"
            if sts.spec.template.spec.service_account_name != expected_sa:
                issue = f"StatefulSet should use ServiceAccount '{expected_sa}', got '{sts.spec.template.spec.service_account_name}'"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ StatefulSet uses correct ServiceAccount")

            # Verify VolumeClaimTemplates exist
            if not sts.spec.volume_claim_templates or len(sts.spec.volume_claim_templates) == 0:
                issue = "StatefulSet missing VolumeClaimTemplates for persistent storage"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ StatefulSet has VolumeClaimTemplates")

        except ApiException as e:
            if e.status == 404:
                issue = f"StatefulSet '{cluster_name}' not found"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                issue = f"Error checking StatefulSet: {e.reason}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
        except Exception as e:
            issue = f"Error checking StatefulSet: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Step 4: Check PodDisruptionBudget
        print(f"\n[4/4] Checking PodDisruptionBudget...")
        try:
            from kubernetes import client

            policy_v1_api = client.PolicyV1Api()
            pdb_name = f"{cluster_name}-budget"

            pdb = policy_v1_api.read_namespaced_pod_disruption_budget(name=pdb_name, namespace=namespace)

            # Verify it has min_available or max_unavailable set
            if pdb.spec.min_available is None and pdb.spec.max_unavailable is None:
                issue = "PodDisruptionBudget should have either minAvailable or maxUnavailable set"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                if pdb.spec.min_available is not None:
                    print(f"  ✅ PodDisruptionBudget has minAvailable={pdb.spec.min_available}")
                else:
                    print(f"  ✅ PodDisruptionBudget has maxUnavailable={pdb.spec.max_unavailable}")

            # Verify selector matches StatefulSet pods
            if pdb.spec.selector is None or pdb.spec.selector.match_labels is None:
                issue = "PodDisruptionBudget missing selector"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                print(f"  ✅ PodDisruptionBudget has selector")

        except ApiException as e:
            if e.status == 404:
                issue = f"PodDisruptionBudget '{pdb_name}' not found"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
            else:
                issue = f"Error checking PodDisruptionBudget: {e.reason}"
                print(f"  ❌ {issue}")
                results["issues"].append(issue)
                results["success"] = False
        except Exception as e:
            issue = f"Error checking PodDisruptionBudget: {str(e)}"
            print(f"  ❌ {issue}")
            results["issues"].append(issue)
            results["success"] = False

        # Final summary
        print(f"\n{'='*60}")
        if results["success"]:
            print("✅ Deploy successful: All CockroachDB cluster resources created correctly")
        else:
            print(f"❌ Deploy failed: {len(results['issues'])} issue(s) found")
            for i, issue in enumerate(results["issues"], 1):
                print(f"   {i}. {issue}")
        print(f"{'='*60}\n")

        return results
