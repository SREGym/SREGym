#https://www.nathanobert.com/posts/blog-kubernetes-on-ubuntu/
#https://earthly.dev/blog/deploy-kubernetes-cri-o-container-runtime/
#https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/


# refresh the package list (info about available packages and their version) from the repositories
sudo apt update -y

# upgrade installed packages to their latest version
sudo apt upgrade -y


# removes any automatically installed packages that are not longer needed



# install docker - https://docs.docker.com/engine/install/ubuntu/ (follow the docker page)

# remove unofficial versions of docker if any
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do sudo apt-get remove $pkg; done


#Set up Docker's apt repository.

sudo apt update
sudo apt install ca-certificates curl
# Creates a directory (/etc/apt/keyrings) to store the GPG key. (install doesn't install anything - https://linuxhandbook.com/install-command/)
sudo install -m 0755 -d /etc/apt/keyrings
# download Docker's official GPG keys
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
# set read permissions for all users on the key file.
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources:
# deb is the format, as well as filename extension of the software package format for the Debian Linux distribution and its derivatives.
# this line tells APT to use the specified Docker repository for Ubuntu 22.04 (Jammy) with the appropriate architecture and GPG key verification
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# refresh the package list with the newly added repository
sudo apt update

# install the packages

sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# start service if docker is not running

sudo systemctl status docker.service --no-pager;
sudo service docker start

# test if docker is properly installed
# https://docs.docker.com/engine/install/linux-postinstall/, https://askubuntu.com/questions/477551/how-can-i-use-docker-without-sudo
# The Docker daemon (the core component of Docker) communicates with the Docker CLI (command-line interface) via a Unix socket (not a TCP port).
# By default, this Unix socket is owned by the root user.
# Without proper permissions, non-root users cannot access this socket. Docker daemon while creating the socket, creates a group called docker 
# with #read/write privileges. So we can skip using sudo before every command by adding the user to docker group
sudo usermod -aG docker $USER

# for the above command to take effect, we need to start a new session. If we don't want to do that, we can use newgrp to start a new session with
# with the specified group as the group ID. Only needed for debugging.
# newgrp docker && docker run hello-world


# Install cri-dockerd - the driver between Kubernetes CRI interface and the docker APIs
# pick the latest stable release from here -
# wget https://github.com/Mirantis/cri-dockerd/releases/download/<version>/cri-dockerd_<>.deb
wget https://github.com/Mirantis/cri-dockerd/releases/download/v0.3.12/cri-dockerd_0.3.12.3-0.ubuntu-jammy_amd64.deb
sudo apt install ./cri-dockerd_0.3.12.3-0.ubuntu-jammy_amd64.deb -y


sudo systemctl status docker.service --no-pager;
sudo systemctl status cri-docker.service --no-pager;




# kubeadm installation - https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/
# apt-transport-https may be a dummy package; if so, you can skip that package
sudo apt-get install -y apt-transport-https ca-certificates curl gpg

# add kube repository
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /" | sudo tee /etc/apt/sources.list.d/kubernetes.list


#Download the public signing key for the Kubernetes package repositories. The same signing key is used for all repositories, so you can disregard #the version in the URL:

curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg


# update the packages
sudo apt update;

# install packages
# Don't pass "-y" flag as packages could already be installed and held at a particular version
# sudo apt-get install -qy kubeadm kubelet kubectl --allow-change-held-packages
sudo apt-get install -qy kubeadm kubelet kubectl


# avoids the package being automatically updated. Good for stability
sudo apt-mark hold kubeadm kubelet kubectl;
kubeadm version;


# disable swap
sudo swapoff -a
sudo sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab

# check if swap is disabled
free -m


#Enable the necessary kernel modules: overlay & br_netfilter

sudo modprobe overlay
sudo modprobe br_netfilter

#create script to load modules on every reboot
#heredoc example - https://stackoverflow.com/questions/2500436/how-does-cat-eof-work-in-bash

cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF


# these are the default values of these kernetl parameters but they need to be set to these values for Kubernetes to function properly
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

sudo sysctl --system