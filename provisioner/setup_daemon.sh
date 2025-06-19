#!/bin/bash

# Exit on error
set -e

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root"
    exit 1
fi

# Get the absolute path of the provisioner directory
PROVISIONER_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(dirname "$PROVISIONER_DIR")
VENV_PATH="$ROOT_DIR/.venv"

# Create the service file
echo "Creating systemd service file..."
cat > /etc/systemd/system/provisioner.service << EOF
[Unit]
Description=Cloudlab Provisioner Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROVISIONER_DIR
ExecStart=$VENV_PATH/bin/python $PROVISIONER_DIR/daemon.py
Restart=on-failure
RestartSec=5
Environment=PYTHONPATH=$ROOT_DIR:$PROVISIONER_DIR

[Install]
WantedBy=multi-user.target
EOF

# Set proper permissions
chmod 644 /etc/systemd/system/provisioner.service

# Reload systemd to recognize new service
echo "Reloading systemd..."
systemctl daemon-reload

# Stop if already running
echo "Stopping service..."
systemctl stop provisioner.service

# Start the service
echo "Starting service..."
systemctl start provisioner.service

# Check status
echo "Checking service status..."
systemctl status provisioner.service

echo "Setup complete! The provisioner daemon should now be running."
echo "You can check the logs using:"
echo "  - journalctl -u provisioner.service -f" 