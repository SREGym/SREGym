#!/bin/bash

# Function to generate an SSH key if not already present
generate_ssh_key() {
    if [ ! -f ~/.ssh/id_rsa ]; then
        echo "SSH key not found. Generating one..."
        ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
        echo "SSH key generated."
    else
        echo "SSH key already exists."
    fi
}

# Generate the SSH key if it's not already there
generate_ssh_key

# AIOpsLab setup
git clone https://github.com/microsoft/AIOpsLab.git
cd AIOpsLab
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.11
sudo apt-get install -y python3.11-venv
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
.venv/bin/pip install -U pip setuptools
.venv/bin/pip install poetry
.venv/bin/poetry install

# DeathStarBench prerequisites
sudo apt install -yq python3-pip datamash libssl-dev libz-dev luarocks python3-pip unzip
pip3 install -q crossplane pandas PyYAML numpy kubernetes scikit-optimize aiohttp
sudo luarocks install luasocket


# Install Helm
sudo snap install helm --classic


