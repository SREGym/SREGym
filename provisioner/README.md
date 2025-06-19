# CloudLab Provisioner

A tool for automatically provisioning and managing clusters on CloudLab. This provisioner helps maintain a pool of available clusters and handles cluster lifecycle management including claiming, extending, and automatic cleanup.

## Features

- Automatic cluster provisioning and management
- User registration and cluster claiming
- Automatic cluster extension for active users
- Automatic cleanup of inactive clusters
- Email notifications for cluster events
- CLI interface for easy interaction

## Prerequisites

1. Go to https://www.cloudlab.us/
2. Login with your cloudlab account
3. On the top right corner, click on your username, and then click on "Download Credentials"
4. This will take you to a page with a button to download the credentials. Click on it.
5. This will download a file called `cloudlab.pem`.

The `cloudlab.pem` contains the encrypted private key to your cloudlab account and ssl certificate. You need to decrypt it before using it.

### Install OpenSSL (if not already installed)

For Ubuntu/Debian:
```bash
sudo apt install openssl
```

For macOS:
```bash
brew install openssl
```

### Decrypting the CloudLab Credentials

```bash
openssl rsa -in cloudlab.pem -out cloudlab_decrypted.pem
```

When prompted for a password, enter your CloudLab account password (the same one you use to login to the CloudLab website).
This will create a new file `cloudlab_decrypted.pem` containing your decrypted private key.
The SSL certificate remains in the original `cloudlab.pem` file.

### Environment Variables

The provisioner needs its own set of ssh keys. Generate ssh keys for the provisioner using the following command:

```bash
ssh-keygen -t ed25519 -f provisioner_ssh_key
```

Set the following required environment variables in `.env` file:

```bash
PROVISIONER_SSH_PRIVATE_KEY_PATH="/path/to/provisioner_ssh_key"
PROVISIONER_SSH_PUBLIC_KEY_PATH="/path/to/provisioner_ssh_key.pub"

CLOUDLAB_CERT_PATH="/path/to/cloudlab.pem"
CLOUDLAB_KEY_PATH="/path/to/cloudlab_decrypted.pem"
CLOUD_PROJECT_NAME="your-cloudlab-project-name"

DEPLOY_KEY_PATH="/path/to/deploy-key
```

Optional email notification settings:

```bash
SMTP_SERVER="smtp.gmail.com"
MTP_PORT="587"
SMTP_USERNAME="your.email@gmail.com"
SMTP_PASSWORD="your-app-password"
```

For Gmail, you'll need to create an app password. Follow this [guide](https://bestsoftware.medium.com/how-to-create-an-app-password-on-gmail-e00eff3af4e0) to create one.

## CloudLab Provisioner

### How It Works

#### Cluster Management

- Maintains 2 unclaimed clusters ready for use
- Unclaimed clusters are deleted after 16 hours of inactivity
- Each user can claim up to 2 clusters
- Maximum of 8 total clusters (claimed + unclaimed)

#### Cluster Lifecycle

1. **Claiming**: Users can claim available clusters. If no clusters clsuter ready to be claimed, the provisioner will create new ones.
2. **Extension**: Claimed clusters are automatically extended for the next 7 days every day.
3. **Cleanup**: Inactive clusters (>48 hours without SSH access) are automatically deleted

#### Daemon Operation

- Runs every set interval (default is 5 minutes)
- Manages cluster lifecycle
- Sends email notifications for important events
- Handles automatic extensions and cleanup

### Running the Provisioner as a Daemon Service

1. Run the setup script:
```bash
chmod +x setup_daemon.sh
sudo ./setup_daemon.sh
```
2. To stop the daemon, run:
```bash
sudo systemctl stop provisioner.service
```

### Running the Provisioner as a Program

To run the provisioner as a program, run the following command: 

```bash
cd provisioner
python3 daemon.py
```

### Configuring the Provisioner

The variables in `config/settings.py` file are used to configure the provisioner. They can be edited to change the provisioner settings.

### Using the CLI

The provisioner provides a command-line interface for managing clusters:

```bash
python3 cli.py --help
```

#### Available Commands

| Command | Description | Example |
|---------|-------------|---------|
| `register` | Register a new user | `python3 cli.py register --email user@example.com --ssh-key ~/.ssh/id_rsa.pub` |
| `claim` | Claim an available cluster | `python3 cli.py claim --email user@example.com` |
| `list` | List clusters for a user | `python3 cli.py list --email user@example.com` |
| `relinquish` | Release a claimed cluster | `python3 cli.py relinquish --email user@example.com --experiment exp-name` |
| `status` | Check cluster status | `python3 cli.py status --experiment exp-name` |

`claim` has two additional options:
- `--deploy-srearena`: Deploys SRE Arena on the claimed cluster
- `--eval-override`: Overrides the evaluation mode for the claimed cluster so that it won't be deleted because of inactivity

## Testing
The `tests/provisioner/test_provisioner.py` file contains a test suite that tests the core functionalities of the provisioner. Set `SET_TEST_VALUES` to `True` in `config/settings.py` to run the tests with test values. The tests provision actual CloudLab clusters for testing, so CloudLab credentials are required. Running the first 8 tests takes approximately 35-40 minutes. The last one `test_srearena_deploy` takes approximately 10-12 minutes.

### Running Tests

To run all tests:
```bash
cd tests/provisioner
python3 pytest test_provisioner.py
```

To run a specific test:
```bash
python3 pytest test_provisioner.py::test_name
```

### Test Suite Overview

The test suite includes the following tests:

1. test_auto_provisioning - Tests automatic cluster provisioning when lower than MIN_AVAILABLE_CLUSTERS
2. test_user_claim_and_relinquish - Tests user cluster claim and release workflow
3. test_max_clusters_per_user - Ensures users can't exceed their cluster limit
4. test_unclaimed_cluster_timeout - Tests automatic cleanup of unused clusters
5. test_max_total_clusters_limit - Tests system-wide cluster limit enforcement
6. test_claimed_cluster_inactivity_timeout - Tests cleanup of inactive claimed clusters
7. test_eval_override_for_inactivity - Tests evaluation mode claimed cluster protection
8. test_claimed_cluster_extension - Tests automatic claimed cluster reservation extension
9. test_srearena_deploy - Tests SRE Arena deployment on a claimed cluster