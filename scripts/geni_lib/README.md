# Getting CloudLab Credentials

1. Go to https://www.cloudlab.us/
2. Login with your cloudlab account
3. On the top right corner, click on your username, and then click on "Download Credentials"
4. This will take you to a page with a button to download the credentials. Click on it.
5. This will download a file called `cloudlab.pem`.

The `cloudlab.pem` contains the encrypted private key to your cloudlab account and ssl certificate. You need to decrypt it before using it.

## Install OpenSSL (if not already installed)

For Ubuntu/Debian:
```bash
sudo apt install openssl
```

For macOS:
```bash
brew install openssl
```

## Decrypt the CloudLab credentials

```bash
openssl rsa -in cloudlab.pem -out cloudlab_decrypted.pem
```

When prompted for a password, enter your CloudLab account password (the same one you use to login to the CloudLab website).
This will create a new file `cloudlab_decrypted.pem` containing your decrypted private key.
The SSL certificate remains in the original `cloudlab.pem` file.

# About `geni-lib` library

The `geni-lib` library is a Python library for interacting with the GENI (Global Environment for Network Innovations) API. It provides a Python interface to manage slices and slivers on GENI-enabled resources. The original library can be found [here](https://gitlab.flux.utah.edu/emulab/geni-lib). For this project, we have made some modifications to the original library to make it python3 compatible as the original library has some python3 context that causes issues when using it in python3.

The modified library can be found in the `scripts/geni-lib/mod/geni-lib-xlab` directory. It will be automatically installed when you run `uv sync` to install the dependencies.

## Building a context definition for CloudLab

To build a context definition, you'll need:
- Your CloudLab certificate (`cloudlab.pem`)
- Your decrypted private key (`cloudlab_decrypted.pem`)
- Your SSH public key
- Your project name (use lowercase to avoid Slice URN conflicts)

Use the following command format:
```bash
build-context --type cloudlab --cert <path_to_cert> --key <path_to_key> --pubkey <path_to_pubkey> --project <project_name>
```

Example:
```bash
build-context --type cloudlab --cert cloudlab.pem --key cloudlab_decrypted.pem --pubkey ~/.ssh/id_ed25519.pub --project aiopslab
```

# How GENI Works

GENI (Global Environment for Network Innovations) and CloudLab use two core concepts for managing experimental resources:

## Understanding Slices and Slivers

### Slice
- A slice is a logical container that groups resources (nodes, links) for a specific experiment
- Think of it as a virtual workspace for organizing resources
- Has an expiration time that can be renewed

### Sliver
- A sliver is a specific allocated resource (node, link, VM) within a slice
- Each sliver exists at a particular physical site (aggregate)
- Examples: A compute node at Wisconsin CloudLab
- Slivers include details like:
  - Node specifications (e.g., c220g5)
  - IP addresses (public and private)
  - SSH access information
- Sliver expiration time cannot exceed its parent slice's expiration time

## Understanding RSpec Files

RSpec files are used to define the resources and their configurations for a slice. We can get them two ways:
1. We can modify the `generate_rspec.py` script to programmatically define our resources and generate the RSpec file corresponding to our resources.
2. We can simply go to cloudlab and copy the rspec of a profile we want to use. Store the rspec files in the `scripts/geni-lib/rspecs` directory.

## Using the GENI Manager

The `genictl.py` script provides a CLI to manage both slices and slivers.

### Available Commands

1. **create-slice**
   - Creates a new slice container for your experiment
   ```bash
   python3 genictl.py create-slice <slice_name> [--hours HOURS] [--description DESCRIPTION]
   ```

2. **create-sliver**
   - Allocates resources in a specific site
   - Saves login information to `<slice_name>.login.info.txt`
   ```bash
   python3 genictl.py create-sliver <slice_name> <rspec_file> --site {utah,clemson,wisconsin}
   ```

3. **sliver-status**
   - Checks the current status of allocated resources
   ```bash
   python3 genictl.py sliver-status <slice_name> --site {utah,clemson,wisconsin}
   ```

4. **renew-slice**
   - Extends the expiration time of a slice
   ```bash
   python3 genictl.py renew-slice <slice_name> [--hours HOURS]
   ```

5. **renew-sliver**
   - Extends the expiration time of resources at a specific site
   - Note: Set sliver expiration slightly less than slice expiration (e.g., 2.9h instead of 3h) to account for command execution delays
   ```bash
   python3 genictl.py renew-sliver <slice_name> [--hours HOURS] --site {utah,clemson,wisconsin}
   ```

6. **list-slices**
   - Shows all active slices and their details
   ```bash
   python3 genictl.py list-slices
   ```

7. **sliver-spec**
   - Shows detailed specifications of allocated resources
   - Includes node specs, IP addresses, and network info
   ```bash
   python3 genictl.py sliver-spec <slice_name> --site {utah,clemson,wisconsin}
   ```

8. **delete-sliver**
   - Removes allocated resources from a slice
   ```bash
   python3 genictl.py delete-sliver <slice_name> --site {utah,clemson,wisconsin}
   ```

9. **get-hardware-info**
   - Gets the hardware information from CloudLab. This is useful to get the hardware information of the nodes available in the different sites.
   ```bash
   python3 genictl.py get-hardware-info
   ```

10. **create-experiment**
    - Creates a quick experiment with the desired hardware type, number of nodes, OS type and duration
    ```bash
    python3 genictl.py create-experiment [--hardware-type HARDWARE_TYPE] [--nodes NODES] [--duration DURATION] [--os-type OS_TYPE] [--ssh-user SSH_USER] [--ssh-key SSH_KEY] [--k8s] [--pod-network-cidr POD_NETWORK_CIDR] [--deploy-sregym] [--deploy-key DEPLOY_KEY]
    ```
    Options:
    - `--hardware-type`: Hardware type (default: c220g5)
    - `--nodes`: Number of nodes (default: 3)
    - `--duration`: Duration in hours (default: 1)
    - `--os-type`: OS image (default: UBUNTU22-64-STD)
    - `--ssh-user`: SSH username
    - `--ssh-key`: SSH private key file
    - `--k8s`: boolean flag to bootstrap Kubernetes after sliver is ready
    - `--pod-network-cidr`: Calico pod CIDR (default: 192.168.0.0/16)
    - `--deploy-sregym`: boolean flag to deploy SREGym after K8s cluster is ready
    - `--deploy-key`: Path to SSH deploy key for SREGym private repo

11. **renew-experiment**
    - Renews both slice and sliver for an experiment
    ```bash
    python3 genictl.py renew-experiment <slice_name> --site {utah,clemson,wisconsin} [--hours HOURS]
    ```

## Quick Test

Under the `tests/geni-lib/` directory, there is a script called `test_experiment_creation.py` that can be used to create a quick experiment.

```bash
cd tests/geni-lib
python3 test_experiment_creation.py
```

This will create a 3-node experiment with 3 c220g5 nodes in the Wisconsin site for 1 hour.
The login info will be saved to a file called `<slice_name>.login.info.txt`.
