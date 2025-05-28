import yaml
from remote import RemoteExecutor 

def load_config(path="config.yml"):
    with open(path) as f:
        return yaml.safe_load(f)

def nodes_reachable(cfg):
    for ip in cfg["nodes"]:
        try:
            exec = RemoteExecutor(ip, cfg["ssh_user"], cfg["ssh_key"])
            exec.close()
        except Exception:
            return False
    return True

def setup_cloudlab_cluster(cfg):
    # TODO: implement install_k8s_components(),
    # init_master(), join_worker() here
    pass

def setup_kind_cluster(cfg):
    import subprocess
    print("CloudLab unreachable; falling back to Kind.")
    subprocess.run([
      "kind", "create", "cluster",
      "--config", cfg["kind_config_x86"],
    ], check=True)

def main():
    cfg = load_config()
    if cfg["mode"] == "cloudlab" and nodes_reachable(cfg):
        setup_cloudlab_cluster(cfg)
    else:
        setup_kind_cluster(cfg)

if __name__ == "__main__":
    main()
