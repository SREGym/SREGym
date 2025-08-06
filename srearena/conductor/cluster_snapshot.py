
import paramiko
import subprocess
import re
import os

def backup_etcd_via_ssh(
    hostname,
    username,
    password=None,
    key_filename=None,
    snapshot_path="etcd-snapshot.db"
):
    """
    Connect to control node via SSH, automatically fetch etcd certificate parameters, and perform etcdctl snapshot save.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname, username=username, password=password, key_filename=key_filename)
    print(f"Connected to {hostname} as to begin etcd backup process")

    try:
        stdin, stdout, stderr = ssh.exec_command('sudo cat /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    
    etcd_yaml = stdout.read().decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", etcd_yaml)
    cert_match = re.search(r"--cert-file=([^\s,]+)", etcd_yaml)
    key_match = re.search(r"--key-file=([^\s,]+)", etcd_yaml)

    if not cacert_match or not cert_match or not key_match:
        raise RuntimeError("Failed to fetch etcd certificate files from etcd.yaml")
    
    cacert = cacert_match.group(1)
    cert = cert_match.group(1)
    key = key_match.group(1)


    # build etcdctl snapshot save command
    etcdctl_cmd = (
        f"sudo ETCDCTL_API=3 etcdctl "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key} "
        f"snapshot save {snapshot_path}"
    )
    # Execute etcdctl snapshot save command
    try:
        stdin, stdout, stderr = ssh.exec_command(etcdctl_cmd)
        backup_out = stdout.read().decode()
        backup_err = stderr.read().decode()

        # Check if backup was successful
        if "Snapshot saved" not in backup_out:
            raise RuntimeError(f"etcdctl snapshot failed: {backup_err or backup_out}")
    except Exception as e:
        raise RuntimeError(f"Failed to execute etcdctl command: {str(e)}")
    
    # Check if snapshot file is usable
    check_snapshot_cmd = f"sudo ETCDCTL_API=3 etcdctl snapshot status {snapshot_path} --write-out=table"
    try:
        stdin, stdout, stderr = ssh.exec_command(check_snapshot_cmd)
        check_out = stdout.read().decode()
        check_err = stderr.read().decode()
        if "Error" in check_err or "Error" in check_out:
            raise RuntimeError(f"Snapshot file is corrupt: {check_err or check_out}")
    except Exception as e:
        raise RuntimeError(f"Failed to check snapshot file: {str(e)}")


    print("etcdctl snapshot save successful:", backup_out)
    ssh.close()

def restore_etcd_via_ssh(
    hostname,
    username,
    password=None,
    key_filename=None,
    snapshot_path="etcd-snapshot.db",
    worker_nodes=None
):
    """
    Connect to control node via SSH, automatically fetch etcd certificate and data directory parameters, and perform etcdctl snapshot restore.
    """
    import time

    if worker_nodes:
        for worker_host in worker_nodes:
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl stop kubelet')
            stdout.channel.recv_exit_status()
            worker_ssh.close()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname, username=username, password=password, key_filename=key_filename)
    print(f"Connected to {hostname} as to begin etcd restore process")

    try:
        stdin, stdout, stderr = ssh.exec_command('sudo cat /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    
    etcd_yaml = stdout.read().decode()

    datadir_match = re.search(r"--data-dir=([^\s,]+)", etcd_yaml)
    if not datadir_match:
        raise RuntimeError("Failed to fetch --data-dir from etcd.yaml")
    data_dir = datadir_match.group(1)

    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", etcd_yaml)
    cert_match = re.search(r"--cert-file=([^\s,]+)", etcd_yaml)
    key_match = re.search(r"--key-file=([^\s,]+)", etcd_yaml)

    if not cacert_match or not cert_match or not key_match:
        raise RuntimeError("Failed to fetch etcd certificate files from etcd.yaml")
    
    cacert = cacert_match.group(1)
    cert = cert_match.group(1)
    key = key_match.group(1)

    listen_client_urls_match = re.search(r"--listen-client-urls=([^\s,]+)", etcd_yaml)
    if not listen_client_urls_match:
        raise RuntimeError("Failed to fetch --listen-client-urls from etcd.yaml")
    listen_client_urls = listen_client_urls_match.group(1) 

    # Stopping etcd, kube-apiserver, kube-scheduler and kube-controller-manager
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo mv /etc/kubernetes/manifests/ /etc/kubernetes/manifestsbak/')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    stdout.channel.recv_exit_status()

    def wait_stop(process_name):
        while True:
            stdin, stdout, stderr = ssh.exec_command(f'sudo crictl ps -a | grep {process_name}')
            status = stdout.read().decode()
            if process_name in status and "Running" in status:
                continue
            break

    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_stop(proc)

    # Stopping kubelet
    stdin, stdout, stderr = ssh.exec_command('sudo systemctl stop kubelet')
    stdout.channel.recv_exit_status()

    # Restoring etcd snapshot
    restore_cmd = (
        f"sudo ETCDCTL_API=3 etcdctl "
        f"snapshot restore {snapshot_path} "
        f"--endpoints={listen_client_urls} "
        f"--data-dir={data_dir} "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key}"
    )

    while True:
        stdin, stdout, stderr = ssh.exec_command(restore_cmd)
        restore_out = stdout.read().decode()
        restore_err = stderr.read().decode()

        # Check for data-dir already exists error
        if 'Error: data-dir "' in restore_err and "exists" in restore_err:
            stdin, stdout, stderr = ssh.exec_command(f"sudo rm -rf {data_dir}")
            stdout.channel.recv_exit_status()
            continue
        # Restoration successful
        if "added member" in restore_out or "added member" in restore_err:
            break
        # Other errors
        raise RuntimeError(f"etcdctl restoration failed: {restore_err or restore_out}")

    # Start kubelet
    stdin, stdout, stderr = ssh.exec_command('sudo systemctl start kubelet')
    stdout.channel.recv_exit_status()

    # restart etcd, kube-apiserver, kube-scheduler and kube-controller-manager
    stdin, stdout, stderr = ssh.exec_command('sudo mv /etc/kubernetes/manifestsbak/ /etc/kubernetes/manifests/')
    stdout.channel.recv_exit_status()

    def wait_start(process_name):
        while True:
            stdin, stdout, stderr = ssh.exec_command(f'sudo crictl ps -a | grep {process_name}')
            status = stdout.read().decode()
            if process_name in status and "Running" in status:
                break

    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_start(proc)

    ssh.close()

    if worker_nodes:
        for worker_host in worker_nodes:
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl start kubelet')
            stdout.channel.recv_exit_status()
            worker_ssh.close()
    
    print("etcd snapshot restore process completed.")


def backup_etcd_local(snapshot_path="etcd-snapshot.db"):
    """
    Fetch etcd certificate parameters automatically on the control node and perform etcdctl snapshot save.
    """

    try:
        etcd_yaml = subprocess.check_output(
        'sudo cat /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", etcd_yaml)
    cert_match = re.search(r"--cert-file=([^\s,]+)", etcd_yaml)
    key_match = re.search(r"--key-file=([^\s,]+)", etcd_yaml)

    if not cacert_match or not cert_match or not key_match:
        raise RuntimeError("Failed to fetch etcd certificate files from etcd.yaml")
    
    cacert = cacert_match.group(1)
    cert = cert_match.group(1)
    key = key_match.group(1)

    # Construct etcdctl snapshot save command
    etcdctl_cmd = (
        f"sudo ETCDCTL_API=3 etcdctl "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key} "
        f"snapshot save {snapshot_path}"
    )
    result = subprocess.run(etcdctl_cmd, shell=True, capture_output=True, text=True)
    if "Snapshot saved" not in result.stdout:
        raise RuntimeError(f"etcdctl snapshot failed: {result.stderr or result.stdout}")

    # Check if the snapshot file is usable
    check_snapshot_cmd = f"sudo ETCDCTL_API=3 etcdctl snapshot status {snapshot_path} --write-out=table"
    check_result = subprocess.run(check_snapshot_cmd, shell=True, capture_output=True, text=True)
    if "Error" in check_result.stderr or "Error" in check_result.stdout:
        raise RuntimeError(f"Snapshot file is corrupt: {check_result.stderr or check_result.stdout}")

    print("etcdctl snapshot successful:", result.stdout)

def restore_etcd_local(username=None, password=None, key_filename=None, snapshot_path="etcd-snapshot.db", worker_nodes=None):
    """
    在 control node 本地自动获取 etcd 证书和数据目录参数，执行 etcdctl 快照恢复。
    """
    import time

    # If there are worker_nodes, stop kubelet on each worker node first
    if worker_nodes:
        for worker_host in worker_nodes:
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl stop kubelet')
            stdout.channel.recv_exit_status()
            worker_ssh.close()

    try:
        etcd_yaml = subprocess.check_output(
        'sudo cat /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    
    datadir_match = re.search(r"--data-dir=([^\s,]+)", etcd_yaml)
    if not datadir_match:
        raise RuntimeError("Failed to fetch --data-dir from etcd.yaml")
    data_dir = datadir_match.group(1)

    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", etcd_yaml)
    cert_match = re.search(r"--cert-file=([^\s,]+)", etcd_yaml)
    key_match = re.search(r"--key-file=([^\s,]+)", etcd_yaml)

    if not cacert_match or not cert_match or not key_match:
        raise RuntimeError("Failed to fetch etcd certificate files from etcd.yaml")
    
    cacert = cacert_match.group(1)
    cert = cert_match.group(1)
    key = key_match.group(1)

    listen_client_urls_match = re.search(r"--listen-client-urls=([^\s,]+)", etcd_yaml)
    if not listen_client_urls_match:
        raise RuntimeError("Failed to fetch --listen-client-urls from etcd.yaml")
    listen_client_urls = listen_client_urls_match.group(1)

    # Stop etcd, kube-apiserver, kube-scheduler and kube-controller-manager
    subprocess.run('sudo mv /etc/kubernetes/manifests/ /etc/kubernetes/manifestsbak/', shell=True, check=True)

    # Wait for related containers to stop
    def wait_stop(process_name):
        while True:
            status = subprocess.getoutput(f"sudo crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                continue
            break

    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_stop(proc)

    # Stop kubelet
    subprocess.run('sudo systemctl stop kubelet', shell=True, check=True)

    # Restore etcd snapshot
    restore_cmd = (
        f"sudo ETCDCTL_API=3 etcdctl "
        f"snapshot restore {snapshot_path} "
        f"--endpoints={listen_client_urls} "
        f"--data-dir={data_dir} "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key}"
    )
    while True:
        result = subprocess.run(restore_cmd, shell=True, capture_output=True, text=True)
        if 'Error: data-dir "' in result.stderr and "exists" in result.stderr:
            subprocess.run(f"sudo rm -rf {data_dir}", shell=True, check=True)
            continue
        if "added member" in result.stdout or "added member" in result.stderr:
            break
        if result.returncode != 0:
            raise RuntimeError(f"etcdctl restoration failed: {result.stderr or result.stdout}")

    # Start kubelet
    subprocess.run('sudo systemctl start kubelet', shell=True, check=True)

    # Restore manifests
    subprocess.run('sudo mv /etc/kubernetes/manifestsbak/ /etc/kubernetes/manifests/', shell=True, check=True)

    # Wait for related containers to start
    def wait_start(process_name):
        while True:
            status = subprocess.getoutput(f"sudo crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                break

    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_start(proc)

    # If there are worker_nodes, restart kubelet
    if worker_nodes:
        for worker_host in worker_nodes:
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl start kubelet')
            stdout.channel.recv_exit_status()
            worker_ssh.close()

    print("etcd restore process completed.")

def install_etcdctl_kind(etcd_container:str):
    #If etcdctl is not installed, install it
    try:
        subprocess.run(f"docker exec -i {etcd_container} etcdctl version", shell=True, check=True)
        print("etcdctl is already installed in the kind container.")
    except subprocess.CalledProcessError:
        print("etcdctl not found in the kind container, installing...")
        install_cmd = f"docker exec -i {etcd_container} apt install -y etcd-client"
        result = subprocess.run(install_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to install etcdctl: {result.stderr or result.stdout}")
        print("etcdctl installed successfully in the kind container.")

def backup_etcd_kind(snapshot_path="etcd-snapshot.db"):
    """
    Backup etcd data in the kind cluster.
    """
    # Get etcd container name
    etcd_container = subprocess.check_output(
        "docker ps --filter 'name=kind-control-plane' --format '{{.Names}}'", shell=True
    ).decode().strip()
    # Check if etcdctl is installed
    install_etcdctl_kind(etcd_container)
    # Get trusted-ca-file、cert-file and key-file
    etcd_yaml = subprocess.check_output(
        "docker exec -i {} cat /etc/kubernetes/manifests/etcd.yaml".format(etcd_container), shell=True
    ).decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", etcd_yaml)
    cert_match = re.search(r"--cert-file=([^\s,]+)", etcd_yaml)
    key_match = re.search(r"--key-file=([^\s,]+)", etcd_yaml)
    if not cacert_match or not cert_match or not key_match:
        raise RuntimeError("Failed to fetch etcd certificate files from etcd.yaml")
    cacert = cacert_match.group(1)
    cert = cert_match.group(1)
    key = key_match.group(1)
    # Construct etcdctl snapshot save command
    etcdctl_cmd = (
        f"docker exec -i {etcd_container} etcdctl "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key} "
        f"snapshot save {snapshot_path}"
    )
    # Execute etcdctl snapshot save command
    result = subprocess.run(etcdctl_cmd, shell=True, capture_output=True, text=True)
    if "Snapshot saved" not in result.stdout:
        raise RuntimeError(f"etcdctl snapshot failed: {result.stderr or result.stdout}")
    # Check if the snapshot file is usable
    check_snapshot_cmd = f"docker exec -i {etcd_container} etcdctl snapshot status {snapshot_path} --write-out=table"
    check_result = subprocess.run(check_snapshot_cmd, shell=True, capture_output=True, text=True)
    if "Error" in check_result.stderr or "Error" in check_result.stdout:
        raise RuntimeError(f"Snapshot file is corrupt: {check_result.stderr or check_result.stdout}")
    print("etcdctl snapshot successful:", result.stdout)

def restore_etcd_kind(snapshot_path="etcd-snapshot.db"):
    # Fetch all worker container names
    worker_containers = subprocess.check_output(
        "docker ps --filter 'name=kind-worker' --format '{{.Names}}'", shell=True
    ).decode().strip().split('\n')
    # Stop kubelet on all worker containers
    for worker in worker_containers:
        subprocess.run(f"docker exec -i {worker} systemctl stop kubelet", shell=True, check=True)
    # Get etcd container name
    etcd_container = subprocess.check_output(
        "docker ps --filter 'name=kind-control-plane' --format '{{.Names}}'", shell=True
    ).decode().strip()
    # Check if etcdctl is installed
    install_etcdctl_kind(etcd_container)
    # Get trusted-ca-file、cert-file and key-file
    etcd_yaml = subprocess.check_output(
        "docker exec -i {} cat /etc/kubernetes/manifests/etcd.yaml".format(etcd_container), shell=True
    ).decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", etcd_yaml)
    cert_match = re.search(r"--cert-file=([^\s,]+)", etcd_yaml)
    key_match = re.search(r"--key-file=([^\s,]+)", etcd_yaml)
    if not cacert_match or not cert_match or not key_match:
        raise RuntimeError("Failed to fetch etcd certificate files from etcd.yaml")
    cacert = cacert_match.group(1)
    cert = cert_match.group(1)
    key = key_match.group(1)
    # Get data-dir
    data_dir_match = re.search(r"--data-dir=([^\s,]+)", etcd_yaml)
    if not data_dir_match:
        raise RuntimeError("Failed to fetch --data-dir from etcd.yaml")
    data_dir = data_dir_match.group(1)
    # Get listen-client-urls
    listen_client_urls_match = re.search(r"--listen-client-urls=([^\s,]+)", etcd_yaml)
    if not listen_client_urls_match:
        raise RuntimeError("Failed to fetch --listen-client-urls from etcd.yaml")
    listen_client_urls = listen_client_urls_match.group(1)
    # Stop etcd, kube-apiserver, kube-scheduler and kube-controller-manager
    subprocess.run('docker exec -i {} mv /etc/kubernetes/manifests/ /etc/kubernetes/manifestsbak/'.format(etcd_container), shell=True, check=True)
    # Wait for related containers to stop
    def wait_stop(process_name):
        while True:
            status = subprocess.getoutput(f"docker exec -i {etcd_container} crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                continue
            break
    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_stop(proc)
    # Stop kubelet

    subprocess.run(f"docker exec -i {etcd_container} systemctl stop kubelet", shell=True, check=True)
    # Restore etcd snapshot
    restore_cmd = (
        f"docker exec -i {etcd_container} etcdctl "
        f"snapshot restore {snapshot_path} "
        f"--endpoints={listen_client_urls} "
        f"--data-dir={data_dir} "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key}"
    )
    while True:
        result = subprocess.run(restore_cmd, shell=True, capture_output=True, text=True)
        if 'Error: data-dir "' in result.stderr and "exists" in result.stderr:
            subprocess.run(f"docker exec -i {etcd_container} rm -rf {data_dir}", shell=True, check=True)
            continue
        if "added member" in result.stdout or "added member" in result.stderr:
            break
        if result.returncode != 0:
            raise RuntimeError(f"etcdctl restoration failed: {result.stderr or result.stdout}")
    # Start kubelet
    subprocess.run(f"docker exec -i {etcd_container} systemctl start kubelet", shell=True, check=True)
    # Restore manifests
    subprocess.run('docker exec -i {} mv /etc/kubernetes/manifestsbak/ /etc/kubernetes/manifests/'.format(etcd_container), shell=True, check=True)
    # Wait for related containers to start
    def wait_start(process_name):
        while True:
            status = subprocess.getoutput(f"docker exec -i {etcd_container} crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                break
    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_start(proc)    
    # Restart kubelet on all worker containers
    for worker in worker_containers:
        subprocess.run(f"docker exec -i {worker} systemctl start kubelet", shell=True, check=True)
    
    print("etcd restore process completed.")

def backup_etcd(hostname=None, 
                username=None, 
                password=None, 
                key_filename=None, 
                snapshot_path="etcd-snapshot.db"):
    """
    Choose the backup method based on the cluster type.
    """
    keyfile=os.path.expanduser(key_filename) if key_filename else None
    if hostname == "localhost":
        backup_etcd_local(snapshot_path=snapshot_path)

    elif hostname == "kind":
        backup_etcd_kind(snapshot_path=snapshot_path)

    else:
        if not hostname or not username:
            raise ValueError("For remote backup, hostname and username must be provided.")
        backup_etcd_via_ssh(hostname, username, password=password, key_filename=keyfile, snapshot_path=snapshot_path)

def restore_etcd(hostname=None, 
                 username=None, 
                 password=None, 
                 key_filename=None, 
                 snapshot_path="etcd-snapshot.db", 
                 worker_nodes=None):
    """
    Choose the restore method based on the cluster type.
    """
    keyfile=os.path.expanduser(key_filename) if key_filename else None
    if not hostname or not username:
            raise ValueError("For restore, hostname and username must be provided.")
    
    if hostname == "localhost":
        restore_etcd_local(username=username, password=password, key_filename=keyfile, snapshot_path=snapshot_path, worker_nodes=worker_nodes)
    
    elif hostname == "kind":
        restore_etcd_kind(snapshot_path=snapshot_path)

    else:
        restore_etcd_via_ssh(hostname, username, password=password, key_filename=keyfile, snapshot_path=snapshot_path, worker_nodes=worker_nodes)

if __name__ == "__main__":
    # Example usage:
    # backup_etcd(
    #     hostname="amd126.utah.cloudlab.us",
    #     username="jqlefty",
    #     key_filename="/home/lefty777/.ssh/id_rsa",
    #     snapshot_path="etcd-snapshot1.db"
    # )
    restore_etcd(
        hostname="amd126.utah.cloudlab.us",
        username="jqlefty",
        key_filename="/home/lefty777/.ssh/id_rsa",
        snapshot_path="etcd-snapshot1.db",
        worker_nodes=["amd127.utah.cloudlab.us", "amd150.utah.cloudlab.us"]
    )