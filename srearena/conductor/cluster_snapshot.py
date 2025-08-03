
import paramiko
import subprocess
import re

def backup_etcd_via_ssh(
    hostname,
    username,
    password=None,
    key_filename=None,
    snapshot_path="etcd-snapshot.db"
):
    """
    通过 SSH 连接到 control node，自动获取 etcd 证书参数并执行 etcdctl 备份。
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname, username=username, password=password, key_filename=key_filename)
    print(f"Connected to {hostname} as to begin etcd backup process")

    # Get --trusted-ca-file
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--trusted-ca-file" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    cacert_line = stdout.read().decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", cacert_line)
    if not cacert_match:
        raise RuntimeError("Failed to fetch --trusted-ca-file")
    cacert = cacert_match.group(1)
    # print(f"CA Certificate: {cacert}")

    # Get --cert-file
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--cert-file" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    cert_line = stdout.read().decode()
    cert_match = re.search(r"--cert-file=([^\s,]+)", cert_line)
    if not cert_match:
        raise RuntimeError("Failed to fetch --cert-file")
    cert = cert_match.group(1)
    # print(f"Certificate: {cert}")

    # Get --key-file
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--key-file" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    key_line = stdout.read().decode()
    key_match = re.search(r"--key-file=([^\s,]+)", key_line)
    if not key_match:
        raise RuntimeError("Failed to fetch --key-file")
    key = key_match.group(1)
    # print(f"Key File: {key}")

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
        # print("Backup Output:", backup_out)
        # print("Backup Error:", backup_err)
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
    通过 SSH 连接到 control node，自动获取 etcd 证书和数据目录参数，执行 etcdctl 快照恢复。
    """
    import time

    if worker_nodes:
        for worker_host in worker_nodes:
            # print(f"Connecting to worker node {worker_host} to stop kubelet...")
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl stop kubelet')
            stdout.channel.recv_exit_status()
            # print(f"kubelet stopped on worker node {worker_host}.")
            worker_ssh.close()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname, username=username, password=password, key_filename=key_filename)
    print(f"Connected to {hostname} as to begin etcd restore process")

    # Get --data-dir
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--data-dir" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    datadir_line = stdout.read().decode()
    datadir_match = re.search(r"--data-dir=([^\s,]+)", datadir_line)
    if not datadir_match:
        raise RuntimeError("Failed to fetch --data-dir")
    data_dir = datadir_match.group(1)
    # print(f"Data Dir: {data_dir}")

    # Get --trusted-ca-file
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--trusted-ca-file" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    cacert_line = stdout.read().decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", cacert_line)
    if not cacert_match:
        raise RuntimeError("Failed to fetch --trusted-ca-file")
    cacert = cacert_match.group(1)
    # print(f"CA Certificate: {cacert}")

    # Get --cert-file
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--cert-file" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    cert_line = stdout.read().decode()
    cert_match = re.search(r"--cert-file=([^\s,]+)", cert_line)
    if not cert_match:
        raise RuntimeError("Failed to fetch --cert-file")
    cert = cert_match.group(1)
    # print(f"Certificate: {cert}")

    # Get --key-file
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--key-file" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    key_line = stdout.read().decode()
    key_match = re.search(r"--key-file=([^\s,]+)", key_line)
    if not key_match:
        raise RuntimeError("Failed to fetch --key-file")
    key = key_match.group(1)
    # print(f"Key File: {key}")

    # Get --listen-client-urls
    try:
        stdin, stdout, stderr = ssh.exec_command('sudo grep "\\--listen-client-urls" /etc/kubernetes/manifests/etcd.yaml')
    except paramiko.SSHException as e:
        raise RuntimeError(f"Failed to execute command: {str(e)}")
    listen_client_urls_line = stdout.read().decode()
    listen_client_urls_match = re.search(r"--listen-client-urls=([^\s,]+)", listen_client_urls_line)
    if not listen_client_urls_match:
        raise RuntimeError("Failed to fetch --listen-client-urls")
    listen_client_urls = listen_client_urls_match.group(1)
    # print(f"Listen Client URLs: {listen_client_urls}")

    # Stopping etcd, kube-apiserver, kube-scheduler and kube-controller-manager
    # print("Stopping etcd, kube-apiserver, kube-scheduler and kube-controller-manager by moving manifest...")
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
    # print("Stopping kubelet...")
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
    # print(f"Restoring etcd snapshot: {restore_cmd}")
    while True:
        stdin, stdout, stderr = ssh.exec_command(restore_cmd)
        restore_out = stdout.read().decode()
        restore_err = stderr.read().decode()
        # print("Restore Output:", restore_out)
        # print("Restore Error:", restore_err)
        # Check for data-dir already exists error
        if 'Error: data-dir "' in restore_err and "exists" in restore_err:
            # print(f"Detected data-dir already exists error, retrying deletion of {data_dir}...")
            stdin, stdout, stderr = ssh.exec_command(f"sudo rm -rf {data_dir}")
            stdout.channel.recv_exit_status()
            continue
        # Restoration successful
        if "added member" in restore_out or "added member" in restore_err:
            break
        # Other errors
        raise RuntimeError(f"etcdctl restoration failed: {restore_err or restore_out}")

    # Start kubelet
    # print("Starting kubelet...")
    stdin, stdout, stderr = ssh.exec_command('sudo systemctl start kubelet')
    stdout.channel.recv_exit_status()

    # restart etcd, kube-apiserver, kube-scheduler and kube-controller-manager
    # print("Restarting etcd, kube-apiserver, kube-scheduler and kube-controller-manager by restoring manifest...")
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
            # print(f"Connecting to worker node {worker_host} to start kubelet...")
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl start kubelet')
            stdout.channel.recv_exit_status()
            # print(f"kubelet started on worker node {worker_host}.")
            worker_ssh.close()
    
    print("etcd snapshot restore process completed.")


def backup_etcd_local(snapshot_path="etcd-snapshot.db"):
    """
    在 control node 本地自动获取 etcd 证书参数并执行 etcdctl 备份。
    """
    # 获取 --trusted-ca-file
    cacert_line = subprocess.check_output(
        'sudo grep "\\--trusted-ca-file" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", cacert_line)
    if not cacert_match:
        raise RuntimeError("Failed to fetch --trusted-ca-file")
    cacert = cacert_match.group(1)

    # 获取 --cert-file
    cert_line = subprocess.check_output(
        'sudo grep "\\--cert-file" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    cert_match = re.search(r"--cert-file=([^\s,]+)", cert_line)
    if not cert_match:
        raise RuntimeError("Failed to fetch --cert-file")
    cert = cert_match.group(1)

    # 获取 --key-file
    key_line = subprocess.check_output(
        'sudo grep "\\--key-file" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    key_match = re.search(r"--key-file=([^\s,]+)", key_line)
    if not key_match:
        raise RuntimeError("Failed to fetch --key-file")
    key = key_match.group(1)

    # 构建并执行 etcdctl snapshot save 命令
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

    # 检查快照文件是否可用
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

    # 如果有 worker_nodes，先停止 kubelet
    if worker_nodes:
        for worker_host in worker_nodes:
            # subprocess.run(
            #     f"ssh {worker_host} 'sudo systemctl stop kubelet'",
            #     shell=True, check=True
            # )
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl stop kubelet')
            stdout.channel.recv_exit_status()
            worker_ssh.close()

    # 获取 --data-dir
    datadir_line = subprocess.check_output(
        'sudo grep "\\--data-dir" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    datadir_match = re.search(r"--data-dir=([^\s,]+)", datadir_line)
    if not datadir_match:
        raise RuntimeError("Failed to fetch --data-dir")
    data_dir = datadir_match.group(1)

    # 获取 --trusted-ca-file
    cacert_line = subprocess.check_output(
        'sudo grep "\\--trusted-ca-file" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    cacert_match = re.search(r"--trusted-ca-file=([^\s,]+)", cacert_line)
    if not cacert_match:
        raise RuntimeError("Failed to fetch --trusted-ca-file")
    cacert = cacert_match.group(1)

    # 获取 --cert-file
    cert_line = subprocess.check_output(
        'sudo grep "\\--cert-file" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    cert_match = re.search(r"--cert-file=([^\s,]+)", cert_line)
    if not cert_match:
        raise RuntimeError("Failed to fetch --cert-file")
    cert = cert_match.group(1)

    # 获取 --key-file
    key_line = subprocess.check_output(
        'sudo grep "\\--key-file" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    key_match = re.search(r"--key-file=([^\s,]+)", key_line)
    if not key_match:
        raise RuntimeError("Failed to fetch --key-file")
    key = key_match.group(1)

    # 获取 --listen-client-urls
    listen_client_urls_line = subprocess.check_output(
        'sudo grep "\\--listen-client-urls" /etc/kubernetes/manifests/etcd.yaml', shell=True
    ).decode()
    listen_client_urls_match = re.search(r"--listen-client-urls=([^\s,]+)", listen_client_urls_line)
    if not listen_client_urls_match:
        raise RuntimeError("Failed to fetch --listen-client-urls")
    listen_client_urls = listen_client_urls_match.group(1)

    # 停止 etcd、kube-apiserver、kube-scheduler、kube-controller-manager
    print("Stopping etcd, kube-apiserver, kube-scheduler and kube-controller-manager by moving manifest...")
    subprocess.run('sudo mv /etc/kubernetes/manifests/ /etc/kubernetes/manifestsbak/', shell=True, check=True)

    # 等待相关容器停止
    def wait_stop(process_name):
        while True:
            status = subprocess.getoutput(f"sudo crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                continue
            break

    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_stop(proc)

    # 停止 kubelet
    print("Stopping kubelet...")
    subprocess.run('sudo systemctl stop kubelet', shell=True, check=True)

    # 恢复 etcd 快照
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

    # 启动 kubelet
    print("Starting kubelet...")
    subprocess.run('sudo systemctl start kubelet', shell=True, check=True)

    # 恢复 manifests
    print("Restarting etcd, kube-apiserver, kube-scheduler and kube-controller-manager by restoring manifest...")
    subprocess.run('sudo mv /etc/kubernetes/manifestsbak/ /etc/kubernetes/manifests/', shell=True, check=True)

    # 等待相关容器启动
    def wait_start(process_name):
        while True:
            status = subprocess.getoutput(f"sudo crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                break

    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_start(proc)

    # 如果有 worker_nodes，重启 kubelet
    if worker_nodes:
        for worker_host in worker_nodes:
            # subprocess.run(
            #     f"ssh {worker_host} 'sudo systemctl start kubelet'",
            #     shell=True, check=True
            # )
            worker_ssh = paramiko.SSHClient()
            worker_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            worker_ssh.connect(worker_host, username=username, password=password, key_filename=key_filename)
            stdin, stdout, stderr = worker_ssh.exec_command('sudo systemctl start kubelet')
            stdout.channel.recv_exit_status()
            worker_ssh.close()

    print("etcd restore process completed.")

def install_etcdctl_kind(etcd_container:str):
    #如果kind容器中没有etcdctl，则在kind容器中使用apt install安装etcdctl
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
    在 kind 集群中备份 etcd 数据。
    """
    # 获取 etcd 容器名称
    etcd_container = subprocess.check_output(
        "docker ps --filter 'name=kind-control-plane' --format '{{.Names}}'", shell=True
    ).decode().strip()
    # 检查 etcdctl 是否已安装
    install_etcdctl_kind(etcd_container)
    # 获取trusted-ca-file、cert-file 和 key-file
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
    # 构建 etcdctl snapshot save 命令
    etcdctl_cmd = (
        f"docker exec -i {etcd_container} etcdctl "
        f"--cacert={cacert} "
        f"--cert={cert} "
        f"--key={key} "
        f"snapshot save {snapshot_path}"
    )
    # 在docker中执行 etcdctl snapshot save 命令
    result = subprocess.run(etcdctl_cmd, shell=True, capture_output=True, text=True)
    if "Snapshot saved" not in result.stdout:
        raise RuntimeError(f"etcdctl snapshot failed: {result.stderr or result.stdout}")
    # 检查快照文件是否可用
    check_snapshot_cmd = f"docker exec -i {etcd_container} etcdctl snapshot status {snapshot_path} --write-out=table"
    check_result = subprocess.run(check_snapshot_cmd, shell=True, capture_output=True, text=True)
    if "Error" in check_result.stderr or "Error" in check_result.stdout:
        raise RuntimeError(f"Snapshot file is corrupt: {check_result.stderr or check_result.stdout}")
    print("etcdctl snapshot successful:", result.stdout)

def restore_etcd_kind(snapshot_path="etcd-snapshot.db"):
    #获取worker容器列表
    worker_containers = subprocess.check_output(
        "docker ps --filter 'name=kind-worker' --format '{{.Names}}'", shell=True
    ).decode().strip().split('\n')
    # 停止所有 worker 容器中的kubelet
    for worker in worker_containers:
        print(f"Stopping kubelet on worker {worker}...")
        subprocess.run(f"docker exec -i {worker} systemctl stop kubelet", shell=True, check=True)
    # 获取 etcd 容器名称
    etcd_container = subprocess.check_output(
        "docker ps --filter 'name=kind-control-plane' --format '{{.Names}}'", shell=True
    ).decode().strip()
    # 检查 etcdctl 是否已安装
    install_etcdctl_kind(etcd_container)
    # 获取 trusted-ca-file、cert-file 和 key-file
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
    # 获取 data-dir
    data_dir_match = re.search(r"--data-dir=([^\s,]+)", etcd_yaml)
    if not data_dir_match:
        raise RuntimeError("Failed to fetch --data-dir from etcd.yaml")
    data_dir = data_dir_match.group(1)
    # 获取 listen-client-urls
    listen_client_urls_match = re.search(r"--listen-client-urls=([^\s,]+)", etcd_yaml)
    if not listen_client_urls_match:
        raise RuntimeError("Failed to fetch --listen-client-urls from etcd.yaml")
    listen_client_urls = listen_client_urls_match.group(1)
    # 停止 etcd、kube-apiserver、kube-scheduler 和 kube-controller-manager
    print("Stopping etcd, kube-apiserver, kube-scheduler and kube-controller-manager by moving manifest...")
    subprocess.run('docker exec -i {} mv /etc/kubernetes/manifests/ /etc/kubernetes/manifestsbak/'.format(etcd_container), shell=True, check=True)
    # 等待相关容器停止
    def wait_stop(process_name):
        while True:
            status = subprocess.getoutput(f"docker exec -i {etcd_container} crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                continue
            break
    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_stop(proc)
    # 停止 kubelet
    print("Stopping kubelet...")
    subprocess.run(f"docker exec -i {etcd_container} systemctl stop kubelet", shell=True, check=True)
    # 恢复 etcd 快照
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
    # 启动 kubelet
    print("Starting kubelet...")
    subprocess.run(f"docker exec -i {etcd_container} systemctl start kubelet", shell=True, check=True)
    # 恢复 manifests
    print("Restarting etcd, kube-apiserver, kube-scheduler and kube-controller-manager by restoring manifest...")
    subprocess.run('docker exec -i {} mv /etc/kubernetes/manifestsbak/ /etc/kubernetes/manifests/'.format(etcd_container), shell=True, check=True)
    # 等待相关容器启动
    def wait_start(process_name):
        while True:
            status = subprocess.getoutput(f"docker exec -i {etcd_container} crictl ps -a | grep {process_name}")
            if process_name in status and "Running" in status:
                break
    for proc in ["etcd", "kube-apiserver", "kube-scheduler", "kube-controller-manager"]:
        wait_start(proc)    
    # 重启所有 worker 容器中的 kubelet
    for worker in worker_containers:
        print(f"Starting kubelet on worker {worker}...")
        subprocess.run(f"docker exec -i {worker} systemctl start kubelet", shell=True, check=True)
    
    print("etcd restore process completed.")

def backup_etcd(control_node="local", 
                hostname=None, 
                username=None, 
                password=None, 
                key_filename=None, 
                snapshot_path="etcd-snapshot.db"):
    """
    Choose the backup method based on the cluster type.
    """
    if control_node == "local":
        backup_etcd_local(snapshot_path=snapshot_path)

    elif control_node == "remote":
        if not hostname or not username:
            raise ValueError("For remote backup, hostname and username must be provided.")
        backup_etcd_via_ssh(hostname, username, password=password, key_filename=key_filename, snapshot_path=snapshot_path)

    elif control_node == "simulated":
        backup_etcd_kind(snapshot_path=snapshot_path)

    else:
        raise ValueError("Unsupported cluster type. Use 'local' or 'remote'.")

def restore_etcd(control_node="local", 
                 hostname=None, 
                 username=None, 
                 password=None, 
                 key_filename=None, 
                 snapshot_path="etcd-snapshot.db", 
                 worker_nodes=None):
    """
    Choose the restore method based on the cluster type.
    """
    if not hostname or not username:
            raise ValueError("For restore, hostname and username must be provided.")
    
    if control_node == "local":
        restore_etcd_local(username=username, password=password, key_filename=key_filename, snapshot_path=snapshot_path, worker_nodes=worker_nodes)
    
    elif control_node == "remote":
        restore_etcd_via_ssh(hostname, username, password=password, key_filename=key_filename, snapshot_path=snapshot_path, worker_nodes=worker_nodes)
    
    elif control_node == "simulated":
        restore_etcd_kind(snapshot_path=snapshot_path)
    
    else:
        raise ValueError("Unsupported cluster type. Use 'local' or 'remote'.")

if __name__ == "__main__":
    # Example usage:
    # backup_etcd(
    #     control_node="simulated",
    #     hostname="amd126.utah.cloudlab.us",
    #     username="jqlefty",
    #     key_filename="/home/lefty777/.ssh/id_rsa",
    #     snapshot_path="etcd-snapshot1.db"
    # )
    restore_etcd(
        control_node="simulated",
        hostname="amd126.utah.cloudlab.us",
        username="jqlefty",
        key_filename="/home/lefty777/.ssh/id_rsa",
        snapshot_path="etcd-snapshot1.db",
        worker_nodes=["amd127.utah.cloudlab.us", "amd150.utah.cloudlab.us"]
    )