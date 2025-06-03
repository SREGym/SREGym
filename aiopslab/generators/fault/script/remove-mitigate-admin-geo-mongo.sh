#!/bin/bash
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


ROOT_USER="root"
ROOT_PWD="root"  

ADMIN_USER="admin"
ADMIN_PWD="admin"  
READ_WRITE_ROLE="readWrite"
TARGET_DB="geo-db"

echo "Recreating admin user..."

# Connect to MongoDB and create the admin user
mongo admin -u $ROOT_USER -p $ROOT_PWD --authenticationDatabase admin \
     --eval "db.createUser({user: '$ADMIN_USER', pwd: '$ADMIN_PWD', roles:[{role:'userAdminAnyDatabase',db:'admin'}]});"

echo "Admin user recreated"

# Grant readWrite role on the target database
echo "Granting readWrite role to $ADMIN_USER on $TARGET_DB database..."
mongo admin -u $ROOT_USER -p $ROOT_PWD --authenticationDatabase admin \
     --eval "db.grantRolesToUser('$ADMIN_USER', [{role: '$READ_WRITE_ROLE', db: '$TARGET_DB'}]);"

echo "Privileges restored successfully"
