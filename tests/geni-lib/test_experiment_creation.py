# Create a 3-node cloudlab experiment with 3 c220g5 nodes for 1 hour

import datetime
import json
import random

import geni.portal as portal
import geni.util
from geni.aggregate.cloudlab import Clemson, Utah, Wisconsin

context = geni.util.loadContext()

# Randomly generate a slice name as they need to be unique
SLICE_NAME = "test-" + str(random.randint(100000, 999999))
DURATION = 1  # (hours)
DESCRIPTION = "Testing experiment creation"
HARDWARE_TYPE = "c220g5"
AGGREGATE = Wisconsin  # c220g5 nodes are only available at Wisconsin
OS_TYPE = "UBUNTU22-64-STD"

# Create a cluster of 3 c220g5 nodes
request = portal.context.makeRequestRSpec()

node1 = request.RawPC("control")
node2 = request.RawPC("compute1")
node3 = request.RawPC("compute2")

# Free to pick different hardware types but make sure to check if the chosen hardware type is available at the chosen aggregate
node1.hardware_type = HARDWARE_TYPE
node2.hardware_type = HARDWARE_TYPE
node3.hardware_type = HARDWARE_TYPE

node1.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{OS_TYPE}"
node2.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{OS_TYPE}"
node3.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{OS_TYPE}"

link1 = request.Link(members=[node1, node2, node3])

### Create the slice
try:
    print(f"Creating slice: {SLICE_NAME}")
    expiration = datetime.datetime.now() + datetime.timedelta(hours=DURATION)
    ret = context.cf.createSlice(context, SLICE_NAME, exp=expiration, desc=DESCRIPTION)
    print(f"Slice created: {SLICE_NAME} for {DURATION} hours\n")
    print(f"Slice Info: {json.dumps(ret, indent=2)}\n")
except Exception as e:
    print(f"Error creating slice: {e}")
    exit(1)

### Create the sliver (actual experiment)
print(f"Creating sliver in slice: {SLICE_NAME}")
try:
    igm = AGGREGATE.createsliver(context, SLICE_NAME, request)
    print(f"Sliver created\n")
except Exception as e:
    print(f"Error creating sliver: {e}")
    exit(1)

print("Your ssh info:")
geni.util.printlogininfo(manifest=igm)

### Save the login info to a file
login_info = geni.util._corelogininfo(igm)
if isinstance(login_info, list):
    login_info = "\n".join(map(str, login_info))
with open(f"{SLICE_NAME}.login.info.txt", "a") as f:
    f.write(f"Slice name: {SLICE_NAME}\n")
    f.write(f"Cluster name: {AGGREGATE.name}\n")
    f.write(f"Duration: {DURATION} hours\n")
    f.write(f"Hardware type: {HARDWARE_TYPE}\n")
    f.write(f"OS type: {OS_TYPE}\n")
    f.write(login_info)
    f.write("\n")
    f.write(f"To delete the experiment, run the following command:\n")
    f.write(f"python3 genictl.py delete-sliver {SLICE_NAME} --site wisconsin\n")
print(f"\nSSH info saved to {SLICE_NAME}.login.info.txt\n")

print(f"Your experiment under slice: {SLICE_NAME} is successfully created for {DURATION} hours\n")
