import time

from sregym.conductor.oracles.base import Oracle


class ValkeyConfigOracle(Oracle):
    """Checks whether the valkey-cart maxmemory misconfiguration has been fixed.

    The fault is injected via a ConfigMap (valkey-cart-config) mounted into the
    valkey-cart deployment. Agents must find and fix the ConfigMap content AND
    ensure the runtime config reflects the fix.

    Scoring:
      - ConfigMap deleted/fixed AND runtime OK → 100 (persistent fix)
      - Runtime OK but ConfigMap still has bad value → 50 (temporary fix only)
      - Neither fixed → 0
    """

    importance = 2.0

    _INJECTED_MAXMEMORY = 1048576  # 1Mi — the injected value

    def evaluate(self, **kwargs) -> dict:
        print("== Valkey Config Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        cm_fixed = False
        runtime_fixed = False

        # Check 1: ConfigMap content — is valkey-cart-config still present with bad maxmemory?
        try:
            output = kubectl.exec_command(
                f"kubectl get configmap valkey-cart-config -n {namespace} -o jsonpath='{{.data.valkey\\.conf}}'"
            )
            if not output or "not found" in output.lower() or "Error" in output:
                cm_fixed = True
                print("[✅] ConfigMap valkey-cart-config is gone — persistent fix confirmed")
            else:
                # Parse maxmemory from the config file content
                for line in output.splitlines():
                    line = line.strip()
                    if line.startswith("maxmemory ") and not line.startswith("maxmemory-policy"):
                        try:
                            value = int(line.split()[1])
                            if value == 0 or value >= 10485760:  # 0=unlimited or >= 10Mi
                                cm_fixed = True
                                print(f"[✅] ConfigMap has reasonable maxmemory: {value}")
                            else:
                                print(f"[❌] ConfigMap still has restrictive maxmemory: {value}")
                        except (ValueError, IndexError):
                            pass
                if not cm_fixed:
                    print(f"[❌] ConfigMap valkey-cart-config still present with bad config")
        except Exception as e:
            print(f"[⚠️] Error checking ConfigMap: {e}")

        # Check 2: Runtime config — what does the running valkey actually report?
        try:
            pods = kubectl.list_pods(namespace)
            valkey_pods = [p.metadata.name for p in pods.items if "valkey-cart" in p.metadata.name]
            if valkey_pods:
                output = kubectl.exec_command(
                    f"kubectl exec -n {namespace} {valkey_pods[0]} -- valkey-cli CONFIG GET maxmemory"
                )
                parts = output.strip().splitlines()
                if len(parts) >= 2:
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

        if cm_fixed and runtime_fixed:
            accuracy = 100.0
        elif runtime_fixed and not cm_fixed:
            accuracy = 50.0  # Runtime-only fix — ConfigMap will revert on next pod restart
        else:
            accuracy = 0.0

        success = accuracy > 0
        print(f"Valkey Config Result: accuracy={accuracy}, success={success}")
        return {"success": success, "accuracy": accuracy}


class CartResourceOracle(Oracle):
    """Checks whether the cart CPU throttle has been removed.

    The fault injects a 50m CPU limit on the cart deployment, starving it of CPU
    under high traffic. Agents must identify and remove/increase this limit.

    Scoring:
      - CPU limit removed or >= 500m → 100
      - CPU limit between 200m and 500m → 60 (improved but still constrained)
      - CPU limit < 200m (still throttled) → 0
    """

    importance = 2.0

    _INJECTED_CPU = "50m"
    _THROTTLE_THRESHOLD_M = 200   # millicores — below this is still throttled
    _GOOD_THRESHOLD_M = 500       # millicores — at or above this is healthy

    def evaluate(self, **kwargs) -> dict:
        print("== Cart Resource Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        try:
            deployment = kubectl.get_deployment("cart", namespace)
            container = deployment.spec.template.spec.containers[0]
            limits = (container.resources.limits or {}) if container.resources else {}
            cpu_limit = limits.get("cpu", None)

            if cpu_limit is None:
                print("[✅] Cart has no CPU limit — resource constraint removed")
                accuracy = 100.0
            else:
                # Parse millicores: "500m" → 500, "1" → 1000
                cpu_str = str(cpu_limit)
                if cpu_str.endswith("m"):
                    cpu_m = int(cpu_str[:-1])
                else:
                    cpu_m = int(float(cpu_str) * 1000)

                if cpu_m >= self._GOOD_THRESHOLD_M:
                    print(f"[✅] Cart CPU limit is adequate: {cpu_limit}")
                    accuracy = 100.0
                elif cpu_m >= self._THROTTLE_THRESHOLD_M:
                    print(f"[⚠️] Cart CPU limit improved but still constrained: {cpu_limit}")
                    accuracy = 60.0
                else:
                    print(f"[❌] Cart CPU limit still throttled: {cpu_limit}")
                    accuracy = 0.0

        except Exception as e:
            print(f"[❌] Error checking cart resources: {e}")
            accuracy = 0.0

        success = accuracy >= 60.0
        print(f"Cart Resource Result: accuracy={accuracy}, success={success}")
        return {"success": success, "accuracy": accuracy}


class AvailabilityDamageOracle(Oracle):
    """Measures availability damage caused by the agent's mitigation actions.

    Checks that the agent didn't "fix" the problem by destroying availability:
    killing the load generator, scaling services to zero, or reducing user traffic.
    Then measures the actual error rate under load and computes a recovery percentage
    against the fault-impact baseline captured at injection time.
    """

    importance = 2.5

    _BUFFER_SECONDS = 15
    _RECENT_WINDOW = 120  # seconds of recent workload to sample

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

        # Check 2: Did the agent reduce LOCUST_USERS below the injected level?
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
        for svc in ["cart", "checkout", "frontend"]:
            try:
                deploy = kubectl.get_deployment(svc, namespace)
                if (deploy.spec.replicas or 0) == 0:
                    print(f"[❌] {svc} scaled to 0 replicas — service unavailable")
                    return self._result(accuracy=0.0, recovery_pct=0.0, reason=f"{svc} scaled to 0")
            except Exception:
                print(f"[❌] {svc} deployment not found")
                return self._result(accuracy=0.0, recovery_pct=0.0, reason=f"{svc} not found")

        # Check 4: Measure post-mitigation error rate from recent workload entries
        print(f"[⏳] Waiting {self._BUFFER_SECONDS}s buffer before sampling workload...")
        time.sleep(self._BUFFER_SECONDS)

        try:
            wrk = self.problem.app.wrk
            wrk._extractlog()
            entries = wrk.recent_entries(duration=self._RECENT_WINDOW)

            if not entries:
                print("[❌] No workload entries in recent window — system may be completely down")
                return self._result(accuracy=0.0, recovery_pct=0.0, reason="no recent workload entries")

            error_count = sum(1 for e in entries if not e.ok)
            total_requests = sum(e.number for e in entries)
            total = len(entries)
            post_error_rate = error_count / total

            print(f"[📊] Post-mitigation workload: {total} entries, {error_count} errors, "
                  f"error_rate={post_error_rate:.2%}, total_requests={total_requests}")

        except Exception as e:
            print(f"[❌] Error collecting workload: {e}")
            return self._result(accuracy=0.0, recovery_pct=0.0, reason=f"workload error: {e}")

        # Compute recovery percentage against fault-impact baseline
        fault_snapshot = getattr(self.problem, "fault_impact_snapshot", None)
        recovery_pct = self._compute_recovery_pct(fault_snapshot, post_error_rate)

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
        if not fault_snapshot:
            print("[⚠️] No fault impact snapshot — cannot compute recovery %")
            return -1.0

        fault_error_rate = fault_snapshot.get("error_rate", 0.0)
        print(f"[📊] Fault baseline error_rate={fault_error_rate:.2%}, post={post_error_rate:.2%}")

        if fault_error_rate <= 0.01:
            recovery_pct = 100.0 if post_error_rate < 0.05 else 0.0
        else:
            recovery_pct = ((fault_error_rate - post_error_rate) / fault_error_rate) * 100.0

        recovery_pct = max(-100.0, min(100.0, recovery_pct))
        label = "improved" if recovery_pct > 0 else ("worsened" if recovery_pct < 0 else "unchanged")
        print(f"[📊] Recovery: {recovery_pct:.1f}% ({label})")
        return recovery_pct

    def _result(self, accuracy: float, recovery_pct: float, reason: str = "", **extra) -> dict:
        success = accuracy >= 70.0
        result = {"success": success, "accuracy": accuracy, "recovery_pct": recovery_pct}
        if reason:
            result["reason"] = reason
        result.update(extra)
        print(f"Availability Damage Result: accuracy={accuracy}, success={success}, recovery={recovery_pct:.1f}%")
        return result
