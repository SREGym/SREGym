"""Manual CloudLab experiment creation helper.

This module is deliberately side-effect free when imported so pytest collection
cannot allocate external infrastructure.
"""

import datetime
import json
import random

import geni.portal as portal
import geni.util
from geni.aggregate.cloudlab import Wisconsin

DURATION = 1  # (hours)
DESCRIPTION = "Testing experiment creation"
HARDWARE_TYPE = "c220g5"
AGGREGATE = Wisconsin  # c220g5 nodes are only available at Wisconsin
OS_TYPE = "UBUNTU22-64-STD"


def main():
    context = geni.util.loadContext()
    slice_name = "test-" + str(random.randint(100000, 999999))
    request = portal.context.makeRequestRSpec()
    nodes = [request.RawPC("control"), request.RawPC("compute1"), request.RawPC("compute2")]
    for node in nodes:
        node.hardware_type = HARDWARE_TYPE
        node.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{OS_TYPE}"
    request.Link(members=nodes)

    print(f"Creating slice: {slice_name}")
    expiration = datetime.datetime.now() + datetime.timedelta(hours=DURATION)
    ret = context.cf.createSlice(context, slice_name, exp=expiration, desc=DESCRIPTION)
    print(f"Slice created: {slice_name} for {DURATION} hours\n")
    print(f"Slice Info: {json.dumps(ret, indent=2)}\n")

    print(f"Creating sliver in slice: {slice_name}")
    igm = AGGREGATE.createsliver(context, slice_name, request)
    print("Sliver created\n")
    print("Your ssh info:")
    geni.util.printlogininfo(manifest=igm)

    login_info = geni.util._corelogininfo(igm)
    if isinstance(login_info, list):
        login_info = "\n".join(map(str, login_info))
    with open(f"{slice_name}.login.info.txt", "a") as file:
        file.write(f"Slice name: {slice_name}\n")
        file.write(f"Cluster name: {AGGREGATE.name}\n")
        file.write(f"Duration: {DURATION} hours\n")
        file.write(f"Hardware type: {HARDWARE_TYPE}\n")
        file.write(f"OS type: {OS_TYPE}\n")
        file.write(login_info)
        file.write("\nTo delete the experiment, run:\n")
        file.write(f"python3 genictl.py delete-sliver {slice_name} --site wisconsin\n")

    print(f"\nSSH info saved to {slice_name}.login.info.txt")
    print(f"Experiment under slice {slice_name} created for {DURATION} hours")


if __name__ == "__main__":
    main()
