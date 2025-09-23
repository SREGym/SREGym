#All below steps only for setting up controller

sudo systemctl enable --now kubelet
sudo kubeadm config images pull  --cri-socket /var/run/cri-dockerd.sock
sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --cri-socket /var/run/cri-dockerd.sock

#copy kubeconfig to user
mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config


# install network 
wget https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
sleep 3
kubectl apply -f kube-flannel.yml
sudo systemctl status kubelet --no-pager


# commands to troubleshoot
ip addr | grep cni
kubectl get pods -n kube-system



