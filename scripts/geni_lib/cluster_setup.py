#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

import yaml

from scripts.geni_lib.remote import RemoteExecutor

CFG_PATH = pathlib.Path("config.yml")


def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def nodes_reachable(cloud: dict, verbose: bool = True) -> bool:
    """Check if all nodes are reachable with better error handling and retries"""
    print(f"Checking {len(cloud['nodes'])} nodes for SSH connectivity...")

    for i, host in enumerate(cloud["nodes"], 1):
        print(f"   [{i}/{len(cloud['nodes'])}] Testing {host}...", end=" ")

        max_retries = 3
        success = False

        for retry in range(max_retries):
            try:
                executor = RemoteExecutor(host, cloud["ssh_user"], cloud.get("ssh_key"))
                rc, stdout, stderr = executor.exec("echo 'SSH test successful'")
                executor.close()

                if rc == 0:
                    print("✅")
                    success = True
                    break
                else:
                    print(f"❌ (command failed: rc={rc})")
                    if verbose and retry == max_retries - 1:
                        print(f"      stdout: {stdout.strip()}")
                        print(f"      stderr: {stderr.strip()}")

            except Exception as e:
                if retry < max_retries - 1:
                    print(".", end="")
                    time.sleep(5)
                else:
                    print(f"❌ ({type(e).__name__}: {str(e)[:80]}...)")
                    if verbose:
                        print(f"      Full error: {e}")

        if not success:
            return False

    print("✅ All nodes reachable!")
    return True


def install_k8s_components(ex: RemoteExecutor) -> None:
    cmds: list[str] = [
        "sudo swapoff -a",
        "sudo sed -i '/ swap / s/^/#/' /etc/fstab",
        "sudo modprobe br_netfilter",
        "sudo modprobe overlay",
        "echo 'net.bridge.bridge-nf-call-iptables=1' | sudo tee /etc/sysctl.d/k8s.conf",
        "echo 'net.bridge.bridge-nf-call-ip6tables=1' | sudo tee -a /etc/sysctl.d/k8s.conf",
        "echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.d/k8s.conf",
        "sudo sysctl --system",
        "sudo apt-get update -qq",
        "sudo apt-get install -yq apt-transport-https ca-certificates curl \
         gnupg lsb-release jq",
        "sudo rm -f /etc/apt/sources.list.d/kubernetes.list",
        "sudo mkdir -p /etc/apt/keyrings",
        "sudo rm -f /etc/apt/keyrings/kubernetes-archive-keyring.gpg",
        "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.28/deb/Release.key -o /tmp/k8s-Release.key",
        "sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/kubernetes-archive-keyring.gpg /tmp/k8s-Release.key",
        "rm /tmp/k8s-Release.key",
        "echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-archive-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.28/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list",
        "sudo apt-get update -qq",
        "sudo apt-get install -yq containerd kubelet kubeadm kubectl",
        "sudo apt-mark hold kubelet kubeadm kubectl",
        "sudo mkdir -p /etc/containerd",
        "sudo containerd config default | sudo tee /etc/containerd/config.toml",
        "sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml",
        "sudo systemctl restart containerd",
        "sudo systemctl enable containerd",
        "sudo update-alternatives --set iptables /usr/sbin/iptables-legacy",
        "sudo kubeadm config images pull --kubernetes-version $(kubeadm version -o short)",
    ]
    for cmd in cmds:
        print(f"      Running: {cmd[:60]}...")
        rc, stdout, err = ex.exec(cmd)  # Removed timeout parameter
        if rc != 0:
            print(f"      Failed command: {cmd}")
            print(f"      Error: {err.strip()}")
            raise RuntimeError(f"[{ex.host}] `{cmd}` failed:\n{err.strip()}")


def _wait_for_api_server(ex: RemoteExecutor, timeout: int = 300) -> None:
    start = time.time()
    print("      waiting for API server to be ready…")
    while time.time() - start < timeout:
        ok, _, _ = ex.exec("kubectl get nodes --request-timeout=5s >/dev/null 2>&1")
        if ok == 0:
            print("      API server is ready!")
            return
        time.sleep(5)
    raise RuntimeError("API server not ready after 300 s")


def _wait_controller_ready(ex: RemoteExecutor, timeout: int = 600) -> None:
    start = time.time()
    print("waiting for controller manager to be ready…")
    while time.time() - start < timeout:
        ok, out, _ = ex.exec(
            "kubectl get pod -n kube-system -l component=kube-controller-manager -o jsonpath='{.items[0].status.conditions[?(@.type==\"Ready\")].status}' 2>/dev/null"
        )
        if ok == 0 and out.strip("'\" ") == "True":
            print("controller manager pod is Ready!")
            return
        ok, _, _ = ex.exec(
            "kubectl get secret -n kube-system -o json 2>/dev/null | jq -e '.items[] | select(.type==\"kubernetes.io/service-account-token\")' >/dev/null 2>&1"
        )
        if ok == 0:
            print("ServiceAccount token found!")
            return
        time.sleep(5)
    raise RuntimeError("controller manager not Ready within 10 min")


def _apply_calico_operator(ex: RemoteExecutor) -> None:
    print("applying Tigera operator (server side)…")
    operator_url = "https://raw.githubusercontent.com/projectcalico/calico/v3.27.4/manifests/tigera-operator.yaml"
    rc, _, err = ex.exec(f"kubectl apply --server-side --force-conflicts -f {operator_url}")
    if rc != 0:
        raise RuntimeError(f"Applying tigera‑operator failed:\n{err.strip()}")

    # Wait until CRD shows up
    for _ in range(60):
        ok, _, _ = ex.exec("kubectl get crd installations.operator.tigera.io >/dev/null 2>&1")
        if ok == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError("Installation CRD never appeared")

    print("applying Calico custom resources…")
    cr_url = "https://raw.githubusercontent.com/projectcalico/calico/v3.27.4/manifests/custom-resources.yaml"
    rc, _, err = ex.exec(f"kubectl apply -f {cr_url}")
    if rc != 0:
        raise RuntimeError(f"Applying custom-resources failed:\n{err.strip()}")

    print("      Calico install initiated…")


def init_master(ex: RemoteExecutor, cidr: str) -> str:
    print(f"  ->cleaning previous state on {ex.host}…")
    for cmd in [
        "sudo kubeadm reset -f >/dev/null 2>&1 || true",
        "sudo rm -rf /etc/kubernetes/pki || true",
        "sudo rm -rf /etc/kubernetes/manifests/*.yaml /var/lib/etcd/* || true",
        "sudo rm -rf /etc/cni/net.d/* || true",
        "sudo systemctl restart containerd",
        "sudo systemctl restart kubelet",
    ]:
        ex.exec(cmd)

    print(f"   -> running kubeadm init on {ex.host} …")
    rc, out, err = ex.exec(f"sudo kubeadm init --pod-network-cidr={cidr} --upload-certs --v=5")
    if rc != 0:
        raise RuntimeError(f"[{ex.host}] kubeadm init failed:\n{err.strip()}")

    ex.exec("mkdir -p $HOME/.kube")
    ex.exec("sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config")
    ex.exec("sudo chown $(id -u):$(id -g) $HOME/.kube/config")

    _wait_for_api_server(ex)
    _wait_controller_ready(ex)
    _apply_calico_operator(ex)

    print("      generating join command…")
    for _ in range(30):
        rc, join_cmd, _ = ex.exec("sudo kubeadm token create --print-join-command --ttl 24h")
        if rc == 0 and join_cmd.strip():
            return join_cmd.strip()
        time.sleep(10)
    for line in out.splitlines():
        if line.strip().startswith("kubeadm join"):
            return " ".join(part.rstrip("\\") for part in line.split())
    raise RuntimeError("timed‑out fetching join command")


def join_worker(ex: RemoteExecutor, join_cmd: str) -> None:
    print(f"   ↳ preparing worker {ex.host}…")
    for cmd in [
        "sudo kubeadm reset -f >/dev/null 2>&1 || true",
        "sudo rm -rf /etc/kubernetes/pki || true",
        "sudo rm -rf /etc/kubernetes/manifests/*.yaml /var/lib/etcd/* /var/lib/kubelet/* || true",
        "sudo rm -rf /etc/cni/net.d/* || true",
        "sudo systemctl restart kubelet",
    ]:
        ex.exec(cmd)

    print(f"   ↳ joining {ex.host} to cluster…")
    rc, _, err = ex.exec(f"sudo {join_cmd}")
    if rc != 0:
        raise RuntimeError(f"[{ex.host}] kubeadm join failed:\n{err.strip()}")


def setup_cloudlab_cluster(cfg: dict) -> None:
    cloud, cidr = cfg["cloudlab"], cfg["pod_network_cidr"]
    executors: list[RemoteExecutor] = []
    try:
        for host in cloud["nodes"]:
            print(f"Installing K8s components on {host} …")
            ex = RemoteExecutor(host, cloud["ssh_user"], cloud.get("ssh_key"))
            install_k8s_components(ex)
            executors.append(ex)

        print("\nInitializing control plane…")
        join_cmd = init_master(executors[0], cidr)
        print("✓ Control plane is Ready!")

        if len(executors) > 1:
            print(f"\nJoining {len(executors)-1} workers…")
            for ex in executors[1:]:
                join_worker(ex, join_cmd)

        # health check
        print("\nPerforming cluster health check…")
        time.sleep(10)
        rc, nodes_out, _ = executors[0].exec("kubectl get nodes --no-headers")
        if rc == 0:
            print("\n🟢 Cluster is up:")
            print(nodes_out)
        else:
            print("⚠️  Unable to list nodes — check manually.")
    finally:
        for ex in executors:
            ex.close()


def setup_kind_cluster(cfg: dict) -> None:
    print("CloudLab unreachable — creating local Kind cluster…")
    kind_cfg = cfg["kind"]["kind_config_arm"]  # adjust arch detection if needed
    subprocess.run(["kind", "create", "cluster", "--config", kind_cfg], check=True)
    print("Kind cluster ready ")


def deploy_srearena(ex: RemoteExecutor, deploy_key_path: str) -> None:
    """Deploy SREArena with proper SSH key handling and host verification"""
    print("Setting up SREArena deployment…")

    # Read the private key content from local file
    try:
        with open(deploy_key_path, "r") as f:
            private_key_content = f.read()
    except FileNotFoundError:
        raise RuntimeError(f"Deploy key not found: {deploy_key_path}")

    # Create the private key on the remote server
    setup_cmds = [
        "mkdir -p ~/.ssh",
        "chmod 700 ~/.ssh",
        # Write the private key to remote server
        f"cat > ~/.ssh/srearena_deploy << 'EOF'\n{private_key_content}\nEOF",
        "chmod 600 ~/.ssh/srearena_deploy",
        # Add GitHub to known_hosts to avoid host key verification
        "ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null || true",
    ]

    for cmd in setup_cmds:
        print(f"      Setting up SSH: {cmd[:50]}...")
        rc, stdout, stderr = ex.exec(cmd)
        if rc != 0:
            print(f"      Setup failed: {stderr.strip()}")
            raise RuntimeError(f"SSH setup failed: {stderr.strip()}")

    # Clone and deploy SREArena
    deploy_cmds = [
        # Use the correct repository URL
        "ssh-agent bash -c 'ssh-add ~/.ssh/srearena_deploy; git clone --recurse-submodules git@github.com:xlab-uiuc/SREArena.git /tmp/srearena'",
        "cd /tmp/srearena",
        # Clean up the private key for security
        "rm -f ~/.ssh/srearena_deploy",
    ]

    for cmd in deploy_cmds:
        print(f"      Running: {cmd[:60]}...")
        rc, stdout, stderr = ex.exec(cmd)
        if rc != 0:
            print(f"      Failed command: {cmd}")
            print(f"      Error: {stderr.strip()}")
            raise RuntimeError(f"[{ex.host}] `{cmd}` failed:\n{stderr.strip()}")

    print("✅ SREArena deployed successfully!")


def setup_cloudlab_cluster_with_srearena(cfg: dict) -> None:
    cloud, cidr = cfg["cloudlab"], cfg["pod_network_cidr"]
    deploy_key = cfg["deploy_key"]
    executors: list[RemoteExecutor] = []
    try:
        for host in cloud["nodes"]:
            print(f"Installing K8s components on {host} …")
            ex = RemoteExecutor(host, cloud["ssh_user"], cloud.get("ssh_key"))
            install_k8s_components(ex)
            executors.append(ex)

        print("\nInitializing control plane…")
        join_cmd = init_master(executors[0], cidr)
        print("✓ Control plane is Ready!")

        if len(executors) > 1:
            print(f"\nJoining {len(executors)-1} workers…")
            for ex in executors[1:]:
                join_worker(ex, join_cmd)

        # Deploy SREArena
        print("\nDeploying SREArena…")
        deploy_srearena(executors[0], deploy_key)

        # Health check
        print("\nPerforming cluster health check…")
        time.sleep(10)
        rc, nodes_out, _ = executors[0].exec("kubectl get nodes --no-headers")
        if rc == 0:
            print("\n🟢 Cluster is up:")
            print(nodes_out)
        else:
            print("⚠️  Unable to list nodes — check manually.")
    finally:
        for ex in executors:
            ex.close()


def main() -> None:
    cfg = load_cfg()
    try:
        if cfg["mode"] == "cloudlab" and nodes_reachable(cfg["cloudlab"]):
            setup_cloudlab_cluster(cfg)
        else:
            setup_kind_cluster(cfg)
    except RuntimeError as exc:
        print(f"\n X  {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️  Setup interrupted by user", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
