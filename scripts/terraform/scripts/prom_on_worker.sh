#!/bin/sh
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

echo "Checking /datadrive/prometheus directory"
if [ ! -d "/datadrive/prometheus" ]; then
    echo "   /datadrive/prometheus does not exist. Creating the directory structure."
    
    # Check if /datadrive exists; if not, create it
    if [ ! -d "/datadrive" ]; then
        sudo mkdir -p /datadrive
        echo "   Created /datadrive directory."
    fi
    
    # Create /datadrive/prometheus
    sudo mkdir -p /datadrive/prometheus
    echo "   Created /datadrive/prometheus directory."
else
    echo "   /datadrive/prometheus directory already exists."
fi