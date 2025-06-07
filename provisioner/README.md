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

Set the following required environment variables:

```bash
export PROVISIONER_SSH_PRIVATE_KEY_PATH="/path/to/provisioner_ssh_key"
export PROVISIONER_SSH_PUBLIC_KEY_PATH="/path/to/provisioner_ssh_key.pub"

export CLOUDLAB_CERT_PATH="/path/to/cloudlab.pem"
export CLOUDLAB_KEY_PATH="/path/to/cloudlab_decrypted.pem"
export CLOUD_PROJECT_NAME="your-cloudlab-project-name"
```

Optional email notification settings:

```bash
export SMTP_SERVER="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USERNAME="your.email@gmail.com"
export SMTP_PASSWORD="your-app-password"
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

1. Edit `setup_daemon.sh` to set the correct `USER` and `GROUP` according to the machine you are using to run the provisioner.
2. Run the setup script:
```bash
chmod +x setup_daemon.sh
sudo ./setup_daemon.sh
```
3. To stop the daemon, run:
```bash
sudo systemctl stop provisioner.service
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
