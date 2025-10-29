from sregym.conductor.oracles.base import Oracle

import pandas as pd
import numpy as np
import io

class RPCRetryStormMitigationOracle(Oracle):
    importance = 1.0

    def analyze_latency_trend(self, csv_file_path, threshold_ratio=2.0):
        try:
            # Read CSV file
            df = pd.read_csv(io.StringIO(csv_file_path))

            # Ensure correct column names
            if 'Second' not in df.columns or 'AverageLatency(ns)' not in df.columns:
                print(f"Error: CSV file is missing required columns")
                return False

            # Convert data types
            df['Second'] = pd.to_numeric(df['Second'], errors='coerce')
            df['AverageLatency(ns)'] = pd.to_numeric(df['AverageLatency(ns)'], errors='coerce')

            # Remove invalid data
            df = df.dropna()

            # Extract 5-55 seconds data
            early_period = df[(df['Second'] >= 5) & (df['Second'] <= 55)]

            # Extract 95-119 seconds data
            late_period = df[(df['Second'] >= 95) & (df['Second'] <= 119)]

            # Check if data is sufficient
            if len(early_period) < 30 or len(late_period) < 15:
                print(f"Warning: Insufficient data points, early: {len(early_period)}, late: {len(late_period)}")
                return False

            # Get latency data
            early_latency = early_period['AverageLatency(ns)'].values
            late_latency = late_period['AverageLatency(ns)'].values

            # Calculate statistics
            early_mean = np.mean(early_latency)
            late_mean = np.mean(late_latency)
            early_std = np.std(early_latency)
            late_std = np.std(late_latency)

            print(f"5-55 seconds: Mean = {early_mean:.2f}, Standard Deviation = {early_std:.2f}")
            print(f"95-119 seconds: Mean = {late_mean:.2f}, Standard Deviation = {late_std:.2f}")

            # Standard 1: Mean Comparison
            # If the late mean is significantly higher than the early mean (exceeds the threshold ratio), it is deemed inconsistent
            mean_ratio = late_mean / early_mean if early_mean > 0 else float('inf')
            print(f"Mean Ratio: {mean_ratio:.2f}")

            if mean_ratio > threshold_ratio:
                print(f"Mean difference is too large (Ratio: {mean_ratio:.2f} > {threshold_ratio})")
                return False

            # Standard 2: Coefficient of Variation Comparison
            # Coefficient of Variation = Standard Deviation / Mean, measures relative variability
            early_cv = early_std / early_mean if early_mean > 0 else 0
            late_cv = late_std / late_mean if late_mean > 0 else 0

            print(f"Coefficient of Variation - Early: {early_cv:.4f}, Late: {late_cv:.4f}")

            # If the late coefficient of variation is significantly greater than the early, it is deemed unstable
            if late_cv > early_cv * 5:
                print(f"Late variation is too large (Late: {late_cv:.4f} > Early: {early_cv * 5:.4f})")
                return False

            # Standard 3: Magnitude Comparison
            # Check for significant jumps in magnitude (e.g., from milliseconds to seconds)
            early_median = np.median(early_latency)
            late_median = np.median(late_latency)

            if late_median > early_median * 100:  # Define a significant jump in magnitude
                print(f"Significant jump in magnitude detected: Late median is {late_median/early_median:.1f} times the Early median")
                return False

            # Standard 4: Outlier Detection
            # Check for a large number of high outliers in the late period
            early_q95 = np.percentile(early_latency, 95)
            late_outliers = np.sum(late_latency > early_q95 * 10)  # Define a high outlier as > 10x early Q95
            late_outlier_ratio = late_outliers / len(late_latency)

            print(f"Late outlier ratio: {late_outlier_ratio:.2%}")

            if late_outlier_ratio > 0.3:  # If more than 30% of points are outliers
                print(f"Late outlier ratio is too high")
                return False

            # Standard 5: Persistent Degradation Detection
            # Check if most of the late period latency is significantly higher than the early period
            persistent_degradation_threshold = 3.0  # This multiplier can be adjusted
            persistent_degradation_ratio = 0.8  # 80% of late data points need to meet the condition

            # Calculate the threshold for early latency (e.g., 95th percentile)
            early_threshold = np.percentile(early_latency, 95)

            # Check how many late data points exceed the early threshold by the specified multiplier
            degraded_points = np.sum(late_latency > early_threshold * persistent_degradation_threshold)
            degraded_ratio = degraded_points / len(late_latency)

            print(f"Persistent degradation ratio: {degraded_ratio:.2%} (Points exceeding early 95th percentile by {persistent_degradation_threshold} times)")

            if degraded_ratio > persistent_degradation_ratio:
                print(f"Persistent degradation detected: {degraded_ratio:.2%} > {persistent_degradation_ratio:.2%}")
                return False

            # Optional: Stricter check - use early mean as baseline
            early_mean_threshold = early_mean * persistent_degradation_threshold
            points_above_mean_threshold = np.sum(late_latency > early_mean_threshold)
            ratio_above_mean = points_above_mean_threshold / len(late_latency)

            print(f"Late points exceeding early mean by {persistent_degradation_threshold} times: {ratio_above_mean:.2%}")
            
            if ratio_above_mean > persistent_degradation_ratio:
                print(f"Persistent degradation detected: {ratio_above_mean:.2%} > {persistent_degradation_ratio:.2%}")
                return False

            print("Latency trend is consistent")
            return True
            
        except Exception as e:
            print(f"An error occurred during analysis: {e}")
            return False

    def run_workload(self, problem, kubectl, namespace='default'):
        problem.start_workload()
        job_name = problem.wrk.job_name
        kubectl.wait_for_job_completion(job_name=job_name, namespace=namespace, timeout=1200)
        workentries = problem.wrk.retrievelog()
        workentry = workentries[0] if workentries else None
        print(f"Workload Entry: {workentry}")
        return workentry

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        results = {}
        workentry = self.run_workload(problem=self.problem, kubectl=kubectl)

        not_metastable = self.analyze_latency_trend(workentry.log) if workentry else False

        if not_metastable:
            # Check if all services (not only faulty service) is back to normal (Running)
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                if pod.status.container_statuses:
                    # Check container statuses
                    for container_status in pod.status.container_statuses:
                        if (
                            container_status.state.waiting
                            and container_status.state.waiting.reason == "CrashLoopBackOff"
                        ):
                            print(f"Container {container_status.name} is in CrashLoopBackOff")
                            not_metastable = False
                        elif (
                            container_status.state.terminated
                            and container_status.state.terminated.reason != "Completed"
                        ):
                            print(
                                f"Container {container_status.name} is terminated with reason: {container_status.state.terminated.reason}"
                            )
                            not_metastable = False
                        elif not container_status.ready:
                            print(f"Container {container_status.name} is not ready")
                            not_metastable = False

                if not not_metastable:
                    break

        results["success"] = not_metastable

        print(f"Mitigation Result: {'Pass ✅' if not_metastable else 'Fail ❌'}")

        return results
