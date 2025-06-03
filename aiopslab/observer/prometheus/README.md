#### Install the Prometheus PV:
- Change the ```local.path``` field in PersistentVolume definition of ```prometheus-pv.yml``` to specify the directory on the host node's filesystem where the actual data for the PersistentVolume will be stored.
- Change the ```nodeAffinity``` to ensure the PV is bound to Pods scheduled on the node, e.g., ```yinfangchen-1``` node.
- Apply the Prometheus PV:
```shell
kubectl apply -f prometheus-pv.yml -n observe
```

#### Install Prometheus:
```shell
cd prometheus/ 
helm install prometheus prometheus/ -n observe
```

#### Uninstall Prometheus:
```shell
helm uninstall prometheus -n observe
```