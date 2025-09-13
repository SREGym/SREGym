"""Inject faults at the OS layer."""

# TODO: replace with khaos
import json
import subprocess
import os

import paramiko
from paramiko.client import AutoAddPolicy

import yaml

from srearena.generators.fault.base import FaultInjector
from srearena.generators.fault.helpers import (
    get_pids_by_name,
    hr_mongod_process_names,
    hr_svc_process_names,
    sn_svc_process_names,
)
from srearena.paths import BASE_DIR
from srearena.service.kubectl import KubeCtl


# a script to create a process, keep send SIGTERM to kubelet
KILL_KUBELET_SCRIPT = """
#!/bin/bash
while true; do
    sudo pkill -TERM kubelet
    sleep 3
done
"""

class RemoteOSFaultInjector(FaultInjector):
    def __init__(self):
        self.kubectl = KubeCtl()
        self.pids = {}
    
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
    
    def get_host_info(self) -> (str, str):
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
    # write a script to create a process, keep send SIGTERM to kubelet
        if not self.check_remote_host():
            print("Your cluster is not applicable for this fault injector, It should be remote.")
            return
        self.worker_info = self.get_host_info()
        if not self.worker_info:
            return
        for host, user in self.worker_info.items():
            print(f"Injecting kubelet crash on {host} with user {user}")
            pid = self.inject_script_on_host(host, user, KILL_KUBELET_SCRIPT, "kill_kubelet.sh")
            if pid:
                print(f"Successfully started kubelet killer on {host} with PID {pid}")
                self.pids[host] = pid
            else:
                print(f"Failed to start kubelet killer on {host}")
                return
        return
    
    def recover_kubelet_crash(self):
        for host, pid in self.pids.items():
            print(f"Cleaning up kubelet crash on {host} with PID {pid}")
            self.clean_up_script_on_host(host, self.worker_info[host], pid, "kill_kubelet.sh")
        return
        
    ###### Helpers ######
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
            cmd = f"chmod +x {script_path} && nohup {script_path} > {log_path} 2>&1 & echo $!"
            stdin, stdout, stderr = ssh.exec_command(cmd)
            print(f"Executed command {cmd} on {host}")
            pid = stdout.readline().strip()
            print(f"Read PID from stdout: {pid}")
            print(f"Started {script_name} on {host} with PID {pid}")
            # Store the PID for later cleanup
            return pid
            
        except Exception as e:
            print(f"Failed to start {script_name} on {host}: {e}")
            return None
        finally:
            ssh.close()

    def clean_up_script_on_host(self, host: str, user: str, pid: str, script_name: str):
        """Clean up the script on the remote host."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        script_path = f"/tmp/{script_name}"
        log_path = f"/tmp/{script_name}.log"
        
        try:
            ssh.connect(host, username=user)
            # Kill the process and clean up the script
            cmd = f"kill {pid} 2>/dev/null; rm -f {script_path} {log_path}"
            stdin, stdout, stderr = ssh.exec_command(cmd)
            print(f"Cleaned up {script_name} on {host} (PID {pid})")
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
