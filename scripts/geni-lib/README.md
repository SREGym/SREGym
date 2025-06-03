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

The `genictl.py` script provides an interactive CLI to manage both slices and slivers.

### Interactive Mode

To enter interactive mode:
```bash
cd scripts/geni-lib
python3 genictl.py -i
```

### Available Commands

1. **create-slice**
   - Creates a new slice container for your experiment
   ```bash
   > create-slice
   Enter slice name: test-slice
   Enter expiration time (hours from now, default 1): 2
   Enter slice description (default "CloudLab experiment"): My distributed experiment
   ```

2. **create-sliver**
   - Allocates resources in a specific site
   - Saves login information to `<slice_name>.login.info.txt`
   ```bash
   > create-sliver
   Enter site (utah, clemson, wisconsin): utah
   Enter slice name: test-slice
   Enter path to RSpec file: rspecs/test.xml
   ```

3. **sliver-status**
   - Checks the current status of allocated resources
   ```bash
   > sliver-status
   Enter site (utah, clemson, wisconsin): utah
   Enter slice name: test-slice
   ```

4. **renew-slice**
   - Extends the expiration time of a slice
   ```bash
   > renew-slice
   Enter slice name: test-slice
   Enter new expiration time (hours from now, default 1): 3
   ```

5. **renew-sliver**
   - Extends the expiration time of resources at a specific site
   - Note: Set sliver expiration slightly less than slice expiration (e.g., 2.9h instead of 3h) to account for command execution delays
   ```bash
   > renew-sliver
   Enter site (utah, clemson, wisconsin): utah
   Enter slice name: test-slice
   Enter new expiration time (hours from now, default 1): 2.9
   ```

6. **list-slices**
   - Shows all active slices and their details
   ```bash
   > list-slices
   Output in JSON format? (y/n): n
   ```

7. **sliver-spec**
   - Shows detailed specifications of allocated resources
   - Includes node specs, IP addresses, and network info
   ```bash
   > sliver-spec
   Enter site (utah, clemson, wisconsin): utah
   Enter slice name: test-slice
   ```

8. **delete-sliver**
   - Removes allocated resources from a slice
   ```bash
   > delete-sliver
   Enter site (utah, clemson, wisconsin): utah
   Enter slice name: test-slice
   ```

9. **get-hardware-info**
   - Gets the hardware information from CloudLab. This is useful to get the hardware information of the nodes available in the different sites.
   ```bash
   > get-hardware-info
   ```

10. **quick-experiment**
   - Creates a quick experiment with the desired hardware type, number of nodes, OS type and duration. The aggregate will be chosen based on where the hardware type is available.
   ```bash
   > quick-experiment
   Enter hardware type: c220g5
   Enter duration in hours (default 1): 2
   Enter number of nodes (default 3): 5
   Enter OS type (default UBUNTU22-64-STD): UBUNTU20-64-STD
   ```

## Quick Test

Under the `tests/geni-lib/` directory, there is a script called `test_experiment_creation.py` that can be used to create a quick experiment.

```bash
cd tests/geni-lib
python3 test_experiment_creation.py
```

This will create a 3-node experiment with 3 c220g5 nodes in the Wisconsin site for 1 hour.
The login info will be saved to a file called `<slice_name>.login.info.txt`.
