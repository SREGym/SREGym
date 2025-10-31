## Environment Setup

This is the instruction to use Ansible to build a remote cluster for SREGym. We currently use [CloudLab](https://www.cloudlab.us/) but we believe this will work on any servers you have access to.


### 1) Modify the inventory file
```bash
cp inventory.yml.example inventory.yml
```

Modify the IPs and user names in the inventory file accordingly, `inventory.yml`. 

### 2) Run the Ansible playbook
```shell
ansible-playbook -i inventory.yml setup_cluster.yml
```

After these, you should see every node running inside the cluster:
```shell
kubectl get nodes
```

### Common Errors
If you're running into issues from Ansible related to host key authentication, try typing `yes` in your terminal for each node, or proceeding with the following steps:

You can create a file in the same directory as this README called `ansible.cfg` to turn off that warning:
```yaml
[defaults]
host_key_checking = False
```
Be mindful about the security implications of disabling host key checking, if you're not aware ask someone who is.
