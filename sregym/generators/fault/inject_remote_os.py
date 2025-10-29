"""Inject faults at the OS layer."""

# TODO: replace with khaos
import json
import subprocess
import os
import time

import paramiko
from paramiko.client import AutoAddPolicy

import yaml

from sregym.generators.fault.base import FaultInjector
from sregym.generators.fault.helpers import (
    get_pids_by_name,
    hr_mongod_process_names,
    hr_svc_process_names,
    sn_svc_process_names,
)
from sregym.paths import BASE_DIR
from sregym.service.kubectl import KubeCtl


# a script to create a process, keep send SIGTERM to kubelet
KILL_KUBELET_SCRIPT = """
#!/bin/bash
while true; do
    sudo pkill -TERM kubelet
    sleep 1
done
"""

class RemoteOSFaultInjector(FaultInjector):
    def __init__(self):
        self.kubectl = KubeCtl()
    
    def check_remote_host(self):
        # kubectl get nodes -o json, if  (kind-worker) is in the nodes, return False
        cmd = "kubectl get nodes"
        out = self.kubectl.exec_command(cmd)
        if "kind-worker" in out:
            print("You are using Kind.")
            return False
        
        # try to find the script/ansible/inventory.yml, if it does not exist, return False
        if not os.path.exists(f"{BASE_DIR}/../scripts/ansible/inventory.yml"):
            print("Inventory file not found: " + f"{BASE_DIR}/../scripts/ansible/inventory.yml")
            return False
        return True
    
    def get_host_info(self):
        # read the script/ansible/inventory.yml, and return the host info
        worker_info = {}
        with open(f"{BASE_DIR}/../scripts/ansible/inventory.yml", "r") as f:
            inventory = yaml.safe_load(f)
            
            # Extract variables from all.vars
            variables = {}
            if "all" in inventory and "vars" in inventory["all"]:
                variables = inventory["all"]["vars"]
            
            # get all the workers
            if "all" in inventory and "children" in inventory["all"] and "worker_nodes" in inventory["all"]["children"]:
                workers = inventory["all"]["children"]["worker_nodes"]["hosts"]
                for worker in workers:
                    ansible_host = workers[worker]["ansible_host"]
                    ansible_user = workers[worker]["ansible_user"]
                    
                    # Replace variables in ansible_user
                    ansible_user = self._replace_variables(ansible_user, variables)
                    
                    # Skip if variables couldn't be resolved
                    if "{{" in ansible_user:
                        print(f"Warning: Unresolved variables in {worker} user: {ansible_user}")
                        continue
                        
                    worker_info[ansible_host] = ansible_user
                return worker_info
                
        print(f"No worker nodes found in the inventory file, your cluster is not applicable for this fault injector")
        return None
    
    def _replace_variables(self, text: str, variables: dict) -> str:
        """Replace {{ variable_name }} with actual values from variables dict."""
        import re
        
        def replace_var(match):
            var_name = match.group(1).strip()
            if var_name in variables:
                return str(variables[var_name])
            else:
                print(f"Warning: Variable '{var_name}' not found in variables")
                return match.group(0)  # Return original if not found
        
        # Replace {{ variable_name }} patterns
        return re.sub(r'\{\{\s*(\w+)\s*\}\}', replace_var, text)

    def inject_kubelet_crash(self):
    # inject a script to create a process, keep send SIGTERM to kubelet
        if not self.check_remote_host():
            print("Your cluster is not applicable for this fault injector, It should be remote.")
            return
        
        self.worker_info = self.get_host_info()
        if not self.worker_info:
            return

        for host, user in self.worker_info.items():
            if self.exist_script_on_host(host, user, "kill_kubelet.sh"):
                print("Kubelet killer already exists on the host. Cleaning up...")
                self.clean_up_script_on_host(host, user, "kill_kubelet.sh")
            
        for host, user in self.worker_info.items():
            pid = self.inject_script_on_host(host, user, KILL_KUBELET_SCRIPT, "kill_kubelet.sh")
            if pid:
                print(f"Successfully started kubelet killer on {host} with PID {pid}")
            else:
                print(f"Failed to start kubelet killer on {host}")
                return
        return
    
    def recover_kubelet_crash(self):
        if not self.check_remote_host():
            print("No need to clean up.")
            return
        if not hasattr(self, "worker_info"):
            self.worker_info = self.get_host_info()
        for host, user in self.worker_info.items():
            self.clean_up_script_on_host(host, user, "kill_kubelet.sh")
        time.sleep(3)
        return
        
    ###### Helpers ######
    def exist_script_on_host(self, host: str, user: str, script_name: str):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        ssh.connect(host, username=user)
        stdin, stdout, stderr = ssh.exec_command(f"ls /tmp/{script_name}")
        return script_name in stdout.read().decode()
    
    def inject_script_on_host(self, host: str, user: str, script: str, script_name: str):
        # ssh into the host, and write a script to create a process, keep send SIGTERM to kubelet
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        script_path = f"/tmp/{script_name}"
        log_path = f"/tmp/{script_name}.log"
        
        try: 
            ssh.connect(host, username=user)
            # Create a script file on the remote host
            print(f"Connected to {host} with user {user}")
            
            sftp = ssh.open_sftp()
            with sftp.file(script_path, 'w') as f:
                f.write(script)
            sftp.close()
            
            # Make the script executable and run it in background
            # First make it executable
            stdin, stdout, stderr = ssh.exec_command(f"chmod +x {script_path}")
            stdout.channel.recv_exit_status()  # Wait for completion
            
            # Then start the script in background and capture PID
            # Use a more reliable method to get PID
            cmd = f"nohup {script_path} > {log_path} 2>&1 & echo $! > /tmp/{script_name}.pid"
            stdin, stdout, stderr = ssh.exec_command(cmd)
            stdout.channel.recv_exit_status()  # Wait for completion
            
            # Read the PID from the file
            stdin, stdout, stderr = ssh.exec_command(f"cat /tmp/{script_name}.pid")
            pid = stdout.readline().strip()
            print(f"Executed command {cmd} on {host}")
            print(f"Read PID from file: {pid}")
            print(f"Started {script_name} on {host} with PID {pid}")
            # Store the PID for later cleanup
            return pid
            
        except Exception as e:
            print(f"Failed to start {script_name} on {host}: {e}")
            return None
        finally:
            ssh.close()

    def clean_up_script_on_host(self, host: str, user: str, script_name: str):
        """Clean up the script on the remote host."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        script_path = f"/tmp/{script_name}"
        log_path = f"/tmp/{script_name}.log"
        pid_path = f"/tmp/{script_name}.pid"
        
        try:
            ssh.connect(host, username=user)
            
            # First, try to read the PID from the file
            stdin, stdout, stderr = ssh.exec_command(f"cat {pid_path} 2>/dev/null")
            pid = stdout.readline().strip()
            
            if pid:
                # Kill the process and clean up the script
                cmd = f"kill {pid} 2>/dev/null; rm -f {script_path} {log_path} {pid_path}"
                stdin, stdout, stderr = ssh.exec_command(cmd)
                print(f"Cleaned up {script_name} on {host} (PID {pid})")
                print(f"Waiting for kubelet to restart...")
                time.sleep(3)
                
        except Exception as e:
            print(f"Failed to clean up {script_name} on {host}: {e}")
        finally:
            ssh.close()


def main():
    print("Testing RemoteOSFaultInjector")
    injector = RemoteOSFaultInjector()
    print("Injecting kubelet crash...")
    injector.inject_kubelet_crash()
    input("Press Enter to recover kubelet crash")
    print("Recovering kubelet crash...")
    injector.recover_kubelet_crash()


if __name__ == "__main__":
    main()
