"""Interface for helm operations"""

import subprocess
import time
import subprocess, shlex
from srearena.service.kubectl import KubeCtl


class Helm:
    @staticmethod
    def install(**args):
        print("== Helm Install ==")
        release_name = args.get("release_name")
        chart_path   = args.get("chart_path")
        namespace    = args.get("namespace")
        version      = args.get("version")
        extra_args   = args.get("extra_args") or []
        remote_chart = bool(args.get("remote_chart", False))
        repo         = args.get("repo")

        if not release_name or not chart_path or not namespace:
            raise ValueError("Helm.install requires release_name, chart_path and namespace")

        # 本地 chart 先更新依赖（remote_chart=False 才需要）
        if not remote_chart:
            dep_cmd = f"helm dependency update {shlex.quote(chart_path)}"
            dep = subprocess.run(dep_cmd, shell=True, capture_output=True, text=True)
            if dep.returncode != 0:
                print(dep.stdout); print(dep.stderr)
                raise RuntimeError(f"helm dependency update failed for {chart_path}")

        cmd = ["helm", "install", release_name, chart_path, "-n", namespace, "--create-namespace"]

        if version:
            cmd += ["--version", version]

        # 远程 chart 时加 --repo
        if remote_chart and repo:
            cmd += ["--repo", repo]

        if extra_args:
            cmd += list(extra_args)

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            print(proc.stdout.strip())
        if proc.returncode != 0:
            # 失败时抛异常，阻止后续 wait_for_ready 一直等
            print(proc.stderr.strip())
            raise RuntimeError(f"helm install failed (rc={proc.returncode})")

    @staticmethod
    def uninstall(**args):
        print("== Helm Uninstall ==")
        release_name = args.get("release_name")
        namespace    = args.get("namespace")
        if not release_name or not namespace:
            raise ValueError("Helm.uninstall requires release_name and namespace")

        if not Helm.exists_release(release_name, namespace):
            print(f"Release {release_name} does not exist. Skipping uninstall.")
            return

        cmd = ["helm", "uninstall", release_name, "-n", namespace]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            print(proc.stdout.strip())
        if proc.returncode != 0:
            print(proc.stderr.strip())
            raise RuntimeError(f"helm uninstall failed (rc={proc.returncode})")

    @staticmethod
    def exists_release(release_name: str, namespace: str) -> bool:
        # helm status 返回码 0 表示存在
        proc = subprocess.run(["helm", "status", release_name, "-n", namespace],
                              capture_output=True, text=True)
        return proc.returncode == 0
    @staticmethod
    def assert_if_deployed(namespace: str):
        """Assert if all services in the application are deployed

        Args:
            namespace (str): Namespace to check

        Returns:
            bool: True if deployed

        Raises:
            Exception: If not deployed
        """
        kubectl = KubeCtl()
        try:
            kubectl.wait_for_ready(namespace)
        except Exception as e:
            raise e

        return True

    @staticmethod
    def upgrade(**args):
        """Upgrade a helm chart

        Args:
            release_name (str): Name of the release
            chart_path (str): Path to the helm chart
            namespace (str): Namespace to upgrade the chart
            values_file (str): Path to the values.yaml file
            set_values (dict): Key-value pairs for --set options
        """
        print("== Helm Upgrade ==")
        release_name = args.get("release_name")
        chart_path = args.get("chart_path")
        namespace = args.get("namespace")
        values_file = args.get("values_file")
        set_values = args.get("set_values", {})

        command = [
            "helm",
            "upgrade",
            release_name,
            chart_path,
            "-n",
            namespace,
            "-f",
            values_file,
        ]

        # Add --set options if provided
        for key, value in set_values.items():
            command.append("--set")
            command.append(f"{key}={value}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        if error:
            print("Error during helm upgrade:")
            print(error.decode("utf-8"))
        else:
            print("Helm upgrade successful!")
            print(output.decode("utf-8"))

    @staticmethod
    def add_repo(name: str, url: str):
        """Add a Helm repository

        Args:
            name (str): Name of the repository
            url (str): URL of the repository
        """
        print(f"== Helm Repo Add: {name} ==")
        command = f"helm repo add {name} {url}"
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        if error:
            print(f"Error adding helm repo {name}: {error.decode('utf-8')}")
        else:
            print(f"Helm repo {name} added successfully: {output.decode('utf-8')}")


# Example usage
if __name__ == "__main__":
    sn_configs = {
        "release_name": "test-social-network",
        "chart_path": "/home/oppertune/DeathStarBench/socialNetwork/helm-chart/socialnetwork",
        "namespace": "social-network",
    }
    Helm.install(**sn_configs)
    Helm.uninstall(**sn_configs)
