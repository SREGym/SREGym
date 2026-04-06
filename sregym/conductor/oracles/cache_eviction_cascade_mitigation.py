import json
import time

from sregym.conductor.oracles.base import Oracle


class ValkeyConfigOracle(Oracle):
    """Checks whether the valkey-cart maxmemory misconfiguration has been fixed.

    Evaluates both the deployment spec (persistent fix) and runtime config (immediate fix).
    A deployment-level fix scores higher than a runtime-only fix because the latter
    won't survive a pod restart.
    """

    importance = 2.0

    def evaluate(self, **kwargs) -> dict:
        print("== Valkey Config Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        deploy_fixed = False
        runtime_fixed = False

        # Check 1: Deployment spec — does the command still contain --maxmemory restriction?
        try:
            deployment = kubectl.get_deployment("valkey-cart", namespace)
            container = deployment.spec.template.spec.containers[0]
            command = container.command or []
            if "--maxmemory" in command:
                try:
                    idx = command.index("--maxmemory")
                    maxmem_value = int(command[idx + 1])
                    if maxmem_value == 0 or maxmem_value >= 10485760:  # 0 (unlimited) or >= 10Mi
                        deploy_fixed = True
                        print(f"[✅] Deployment command has reasonable maxmemory: {maxmem_value}")
                    else:
                        print(f"[❌] Deployment command still has restrictive maxmemory: {maxmem_value}")
                except (ValueError, IndexError):
                    print("[⚠️] Could not parse maxmemory value from deployment command")
            else:
                deploy_fixed = True
                print("[✅] Deployment command does not contain --maxmemory override")
        except Exception as e:
            print(f"[❌] Error checking deployment: {e}")

        # Check 2: Runtime config — what does valkey actually report?
        try:
            pods = kubectl.list_pods(namespace)
            valkey_pods = [p.metadata.name for p in pods.items if "valkey-cart" in p.metadata.name]
            if valkey_pods:
                cmd = f"kubectl exec -n {namespace} {valkey_pods[0]} -- valkey-cli CONFIG GET maxmemory"
                output = kubectl.exec_command(cmd)
                parts = output.strip().splitlines()
                if len(parts) == 2:
                    maxmem_runtime = int(parts[1])
                    if maxmem_runtime == 0 or maxmem_runtime >= 10485760:
                        runtime_fixed = True
                        print(f"[✅] Runtime maxmemory is reasonable: {maxmem_runtime}")
                    else:
                        print(f"[❌] Runtime maxmemory still restrictive: {maxmem_runtime}")
            else:
                print("[❌] No valkey-cart pod found")
        except Exception as e:
            print(f"[❌] Error checking runtime config: {e}")

        # Scoring
        if deploy_fixed and runtime_fixed:
            accuracy = 100.0
        elif runtime_fixed and not deploy_fixed:
            accuracy = 50.0  # Temporary fix — won't survive restart
        else:
            accuracy = 0.0

        success = accuracy > 0
        print(f"Valkey Config Result: accuracy={accuracy}, success={success}")
        return {"success": success, "accuracy": accuracy}


class AvailabilityDamageOracle(Oracle):
    """Measures availability damage caused by the agent's mitigation actions.

    Checks that the agent didn't "fix" the problem by destroying availability:
    killing the load generator, scaling services to zero, or reducing user traffic.
    Then measures the actual error rate under load and compares against the
    fault-impact baseline to compute a recovery percentage.

    The fault_impact_snapshot is captured by the problem class right after fault
    injection and stored as self.problem.fault_impact_snapshot. It contains:
      - error_rate: float (error rate during fault)
      - total_requests: int
      - total_errors: int
    """

    importance = 2.5

    _BUFFER_SECONDS = 15
    _SAMPLE_ENTRIES = 50

    def evaluate(self, **kwargs) -> dict:
        print("== Availability Damage Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # Check 1: Is the load-generator still running?
        try:
            lg_deploy = kubectl.get_deployment("load-generator", namespace)
            lg_replicas = lg_deploy.spec.replicas or 0
            if lg_replicas == 0:
                print("[❌] Load-generator scaled to 0 — agent turned away all customers")
                return self._result(accuracy=0.0, recovery_pct=0.0, reason="load-generator scaled to 0")
        except Exception:
            print("[❌] Load-generator deployment not found — agent may have deleted it")
            return self._result(accuracy=0.0, recovery_pct=0.0, reason="load-generator deleted")

        # Check 2: Did the agent reduce LOCUST_USERS?
        try:
            container = lg_deploy.spec.template.spec.containers[0]
            env_vars = {e.name: e.value for e in (container.env or [])}
            locust_users = int(env_vars.get("LOCUST_USERS", "10"))
            if locust_users < 100:
                print(f"[⚠️] Agent reduced LOCUST_USERS to {locust_users} — turning away customers")
                return self._result(
                    accuracy=30.0, recovery_pct=0.0,
                    reason=f"LOCUST_USERS reduced to {locust_users}",
                )
        except Exception as e:
            print(f"[⚠️] Could not check LOCUST_USERS: {e}")

        # Check 3: Are critical services still present with replicas > 0?
        critical_services = ["cart", "checkout", "frontend"]
        for svc in critical_services:
            try:
                deploy = kubectl.get_deployment(svc, namespace)
                if (deploy.spec.replicas or 0) == 0:
                    print(f"[❌] {svc} scaled to 0 replicas — service unavailable")
                    return self._result(accuracy=0.0, recovery_pct=0.0, reason=f"{svc} scaled to 0")
            except Exception:
                print(f"[❌] {svc} deployment not found")
                return self._result(accuracy=0.0, recovery_pct=0.0, reason=f"{svc} not found")

        # Check 4: Measure post-mitigation error rate from workload
        print(f"[⏳] Waiting {self._BUFFER_SECONDS}s buffer before sampling workload...")
        time.sleep(self._BUFFER_SECONDS)

        try:
            wrk = self.problem.app.wrk
            wrk.collect(number=1)  # Prime the collection
            entries = wrk.collect(number=self._SAMPLE_ENTRIES)

            if not entries:
                print("[❌] No workload entries collected — system may be completely down")
                return self._result(accuracy=0.0, recovery_pct=0.0, reason="no workload entries")

            error_count = sum(1 for e in entries if not e.ok)
            total_requests = sum(e.number for e in entries)
            total = len(entries)
            post_error_rate = error_count / total

            print(f"[📊] Post-mitigation workload: {total} entries, {error_count} errors, "
                  f"error_rate={post_error_rate:.2%}, total_requests={total_requests}")

        except Exception as e:
            print(f"[❌] Error collecting workload: {e}")
            return self._result(accuracy=0.0, recovery_pct=0.0, reason=f"workload collection error: {e}")

        # Compute recovery percentage against fault-impact baseline
        fault_snapshot = getattr(self.problem, "fault_impact_snapshot", None)
        recovery_pct = self._compute_recovery_pct(fault_snapshot, post_error_rate)

        # Graduated scoring based on post-mitigation error rate
        if post_error_rate < 0.05:
            accuracy = 100.0
        elif post_error_rate < 0.20:
            accuracy = 70.0
        elif post_error_rate < 0.50:
            accuracy = 40.0
        else:
            accuracy = 10.0

        return self._result(
            accuracy=accuracy,
            recovery_pct=recovery_pct,
            post_error_rate=post_error_rate,
            post_total_entries=total,
            post_error_count=error_count,
            post_total_requests=total_requests,
        )

    def _compute_recovery_pct(self, fault_snapshot: dict | None, post_error_rate: float) -> float:
        """Compute how much the system recovered from fault state.

        recovery_pct = (fault_error_rate - post_error_rate) / fault_error_rate * 100
        - 100% means fully recovered (post errors ≈ 0)
        - 0% means no improvement
        - Negative means agent made it worse
        """
        if not fault_snapshot:
            print("[⚠️] No fault impact snapshot available — cannot compute recovery %")
            return -1.0

        fault_error_rate = fault_snapshot.get("error_rate", 0.0)
        print(f"[📊] Fault baseline: error_rate={fault_error_rate:.2%}")
        print(f"[📊] Post-mitigation: error_rate={post_error_rate:.2%}")

        if fault_error_rate <= 0.01:
            # Fault had negligible errors — can't measure recovery meaningfully
            recovery_pct = 100.0 if post_error_rate < 0.05 else 0.0
        else:
            recovery_pct = ((fault_error_rate - post_error_rate) / fault_error_rate) * 100.0

        recovery_pct = max(-100.0, min(100.0, recovery_pct))
        print(f"[📊] Recovery: {recovery_pct:.1f}% "
              f"({'improved' if recovery_pct > 0 else 'worsened' if recovery_pct < 0 else 'unchanged'})")
        return recovery_pct

    def _result(self, accuracy: float, recovery_pct: float, reason: str = "", **extra) -> dict:
        success = accuracy >= 70.0
        result = {
            "success": success,
            "accuracy": accuracy,
            "recovery_pct": recovery_pct,
        }
        if reason:
            result["reason"] = reason
        result.update(extra)

        print(f"Availability Damage Result: accuracy={accuracy}, success={success}, "
              f"recovery={recovery_pct:.1f}%")
        return result
