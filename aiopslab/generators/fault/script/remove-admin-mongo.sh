#!/bin/bash
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


ROOT_USER="root"
ROOT_PWD="root" 

echo "Removing admin user..."

mongo admin -u $ROOT_USER -p $ROOT_PWD --authenticationDatabase admin \
     --eval "db.dropUser('admin');"

echo "Admin user removed successfully"
