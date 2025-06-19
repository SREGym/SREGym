import datetime
import json
import random
import re
import sys
import time
import warnings

import click
import geni.portal as portal
import geni.util
from cluster_setup import setup_cloudlab_cluster, setup_cloudlab_cluster_with_srearena

from provisioner.config.settings import AGGREGATES_MAP
from provisioner.utils.parser import collect_and_parse_hardware_info, parse_sliver_info

warnings.filterwarnings("ignore")

# List of available OS types
OS_TYPES = [
    "UBUNTU22-64-STD",
    "UBUNTU20-64-STD",
    "UBUNTU18-64-STD",
    "UBUNTU16-64-STD",
    "DEBIAN11-64-STD",
    "DEBIAN10-64-STD",
    "FEDORA36-64-STD",
    "CENTOS7-64-STD",
    "CENTOS8-64-STD",
    "RHEL8-64-STD",
]


def validate_hours(ctx, param, value):
    float_value = float(value)
    if float_value <= 0:
        raise click.BadParameter("Hours must be greater than 0")
    return float_value


def create_slice(context, slice_name, hours, description):
    try:
        print(f"Creating slice '{slice_name}'...")
        expiration = datetime.datetime.now() + datetime.timedelta(hours=hours)
        res = context.cf.createSlice(context, slice_name, exp=expiration, desc=description)
        print(f"Slice Info: \n{json.dumps(res, indent=2)}")
        print(f"Slice '{slice_name}' created")
    except Exception as e:
        print(f"Error: {e}")


def create_sliver(context, slice_name, rspec_file, site):
    try:
        print(f"Creating sliver in slice '{slice_name}'...")
        aggregate = get_aggregate(site)
        igm = aggregate.createsliver(context, slice_name, rspec_file)
        geni.util.printlogininfo(manifest=igm)

        # Save the login info to a file
        login_info = geni.util._corelogininfo(igm)
        if isinstance(login_info, list):
            login_info = "\n".join(map(str, login_info))
        with open(f"{slice_name}.login.info.txt", "w") as f:
            f.write(f"Slice name: {slice_name}\n")
            f.write(f"Cluster name: {aggregate.name}\n")
            f.write(login_info)

        print(f"Sliver '{slice_name}' created")
    except Exception as e:
        print(f"Error: {e}")


def get_sliver_status(context, slice_name, site):
    try:
        print("Checking sliver status...")
        aggregate = get_aggregate(site)
        status = aggregate.sliverstatus(context, slice_name)
        print(f"Status: {json.dumps(status, indent=2)}")
    except Exception as e:
        print(f"Error: {e}")


def renew_slice(context, slice_name, hours):
    try:
        print("Renewing slice...")
        new_expiration = datetime.datetime.now() + datetime.timedelta(hours=hours)
        context.cf.renewSlice(context, slice_name, new_expiration)
        print(f"Slice '{slice_name}' renewed")
    except Exception as e:
        print(f"Error: {e}")


def renew_sliver(context, slice_name, hours, site):
    try:
        print("Renewing sliver...")
        aggregate = get_aggregate(site)
        new_expiration = datetime.datetime.now() + datetime.timedelta(hours=hours)
        aggregate.renewsliver(context, slice_name, new_expiration)
        print(f"Sliver '{slice_name}' renewed")
    except Exception as e:
        print(f"Error: {e}")


def list_slices(context):
    try:
        print("Listing slices...")
        res = context.cf.listSlices(context)
        print(json.dumps(res, indent=2))
    except Exception as e:
        print(f"Error: {e}")


def list_sliver_spec(context, slice_name, site):
    try:
        print("Listing slivers...")
        aggregate = get_aggregate(site)
        res = aggregate.listresources(context, slice_name, available=True)

        # Parse and display the information
        sliver_info = parse_sliver_info(res.text)

        print("\nExperiment Information:")
        print(f"Description: {sliver_info['description']}")
        print(f"Expiration: {sliver_info['expiration']}")

        print("\nNodes:")
        for node in sliver_info["nodes"]:
            print(f"\nNode: {node['client_id']}")
            print(f"  Hostname: {node['hostname']}")
            print(f"  Public IP: {node['public_ip']}")
            print(f"  Internal IP: {node['internal_ip']}")
            print(f"  Hardware: {node['hardware']}")
            print(f"  OS Image: {node['os_image']}")

        print("\nLocation:")
        print(f"  Country: {sliver_info['location']['country']}")
        print(f"  Latitude: {sliver_info['location']['latitude']}")
        print(f"  Longitude: {sliver_info['location']['longitude']}")
    except Exception as e:
        print(f"Error: {e}")


def delete_sliver(context, slice_name, site):
    try:
        print(f"Deleting sliver '{slice_name}'...")
        aggregate = get_aggregate(site)
        aggregate.deletesliver(context, slice_name)
        print(f"Sliver '{slice_name}' deleted.")
    except Exception as e:
        print(f"Error: {e}")


def get_aggregate(site):
    return AGGREGATES_MAP.get(site.lower())


def get_hardware_info():
    hardware_info_list = collect_and_parse_hardware_info()
    if hardware_info_list:
        print(f"\n{'Hardware Name':<20} | {'Cluster Name':<30} | {'Total':<7} | {'Free':<7}")
        print("-" * 100)

        for item in hardware_info_list:
            if item["total"] > 0 or item["free"] > 0:
                print(
                    f"{item['hardware_name']:<20} | {item['cluster_name']:<30} | {item['total']:<7} | {item['free']:<7}"
                )
    else:
        print("No hardware information available")


def renew_experiment(context, slice_name, site, hours):
    new_slice_expiration = datetime.datetime.now() + datetime.timedelta(hours=(hours + 1))
    new_sliver_expiration = datetime.datetime.now() + datetime.timedelta(hours=hours)
    try:
        print(f"Renewing slice: {slice_name}")
        context.cf.renewSlice(context, slice_name, new_slice_expiration)
        print(f"Slice '{slice_name}' renewed")

        aggregate = get_aggregate(site)

        print(f"Renewing sliver: {slice_name}")
        aggregate.renewsliver(context, slice_name, new_sliver_expiration)
        print(f"Sliver '{slice_name}' renewed")

        print(f"Your experiment under slice: {slice_name} is successfully renewed for {hours} hours\n")
    except Exception as e:
        print(f"Error: {e}")


# Kubernetes bootstrapper


def _host_list_from_logininfo(logininfo) -> list[str]:
    """
    Extract hostnames from GENI login info.

    Input format examples:
    - "[node0][saleha] c220g5-110426.wisc.cloudlab.us: 22"
    - Raw tuples: (node_name, user, hostname, port)

    Returns list[str] of hostnames.
    """
    hosts: list[str] = []

    for item in logininfo:
        # Case 1: Raw tuple format (node_name, user, hostname, port)
        if isinstance(item, (tuple, list)) and len(item) >= 3:
            # The hostname is at index 2 in the tuple format
            hostname = item[2]
            if hostname and isinstance(hostname, str) and "." in hostname:
                hosts.append(hostname)
            continue

        # Case 2: String format "[nodeX][user] hostname: port"
        if isinstance(item, str):
            # Pattern to match: ] hostname: or ] hostname (space before colon)
            # This will capture the hostname between the last ] and either : or end of string
            pattern = r"\]\s*([^\s\[\]:]+\.(?:wisc\.cloudlab\.us|utah\.cloudlab\.us|clemson\.cloudlab\.us|[a-z0-9.-]+))(?:\s*:|$)"
            match = re.search(pattern, item)
            if match:
                hosts.append(match.group(1))
                continue

            # Fallback pattern for any hostname-like string after ]
            pattern = r"\]\s*([a-zA-Z0-9.-]+\.[a-zA-Z0-9.-]+)"
            match = re.search(pattern, item)
            if match:
                hostname = match.group(1)
                # Make sure it's not just the username
                if "." in hostname and hostname != "saleha":
                    hosts.append(hostname)
                continue

    # Remove duplicates while preserving order
    unique_hosts = []
    for host in hosts:
        if host not in unique_hosts:
            unique_hosts.append(host)

    return unique_hosts


def are_nodes_ready(context, slice_name: str, aggregate_name: str) -> bool:
    try:
        aggregate = get_aggregate(aggregate_name)
        sliver_status = aggregate.sliverstatus(context, slice_name)
        resources = sliver_status.get("geni_resources", [])
        return all(resource.get("pg_status") == "ready" for resource in resources)
    except Exception as e:
        print(f"Error: {e}")
        raise e


def create_experiment(
    context,
    hardware_type,
    nodes,
    duration,
    os_type,
    k8s,
    ssh_user,
    ssh_key,
    pod_network_cidr,
    deploy_srearena,
    deploy_key,
):
    hardware_info_list = collect_and_parse_hardware_info()
    cluster_name = None

    for item in hardware_info_list:
        if item["hardware_name"].strip() == hardware_type.strip():
            if item["total"] >= nodes and item["free"] >= nodes:
                print(f"Creating a {nodes} node cluster of {hardware_type} at {item['cluster_name']}")
                cluster_name = item["cluster_name"]
                break
            else:
                print(f"Not enough {hardware_type} nodes available at {item['cluster_name']}")

    if cluster_name is None:
        print(f"No {hardware_type} nodes available")
        return

    print(f"{hardware_type} is available at {cluster_name}\n")
    aggregate_name = cluster_name.replace("Cloudlab ", "").lower()
    aggregate = get_aggregate(aggregate_name)

    slice_name = f"exp-{random.randint(100000,999999)}"
    expires = datetime.datetime.now() + datetime.timedelta(hours=duration)

    # Build simple RSpec
    req = portal.context.makeRequestRSpec()
    pcs = []
    for i in range(nodes):
        n = req.RawPC(f"node{i}")
        n.hardware_type = hardware_type
        n.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//" f"{os_type}"
        n.routable_control_ip = True
        pcs.append(n)
    req.Link(members=pcs)

    print(f"🔧  Creating slice {slice_name} …")
    context.cf.createSlice(context, slice_name, exp=expires, desc="Quick experiment via genictl")

    print(f"🚜  Allocating sliver on {aggregate_name} …")
    manifest = aggregate.createsliver(context, slice_name, req)

    geni.util.printlogininfo(manifest=manifest)

    # save the manifest to a file
    login_info = geni.util._corelogininfo(manifest)
    with open(f"{slice_name}.experiment.info.json", "w") as f:
        f.write(
            json.dumps(
                {
                    "slice_name": slice_name,
                    "aggregate_name": aggregate_name,
                    "duration": duration,
                    "hardware_type": hardware_type,
                    "nodes": nodes,
                    "os_type": os_type,
                    "k8s": k8s,
                    "deploy_srearena": deploy_srearena,
                    "deploy_key": deploy_key,
                    "pod_network_cidr": pod_network_cidr,
                    "created_at": datetime.datetime.now().isoformat(),
                    "login_info": login_info,
                },
                indent=2,
            )
        )

    if not k8s:
        return  # user didn't ask for Kubernetes

    # ── Kubernetes path ───────────────────────────────────────────────────
    print("\n⚙️  --k8s flag detected → bootstrapping Kubernetes once nodes are reachable")

    logininfo = geni.util._corelogininfo(manifest)
    hosts = _host_list_from_logininfo(logininfo)

    print(f"🔍 Debug: Raw logininfo: {logininfo}")
    print(f"🔍 Debug: Extracted hosts: {hosts}")

    if not hosts:
        sys.exit("❌  Couldn't parse node hostnames from login info")

    # Validate that we got actual hostnames, not usernames
    valid_hosts = []
    for host in hosts:
        if "." in host and not host == ssh_user:
            valid_hosts.append(host)
        else:
            print(f"⚠️  Skipping invalid hostname: {host}")

    if not valid_hosts:
        print("❌  No valid hostnames found! Raw login info:")
        for item in logininfo:
            print(f"    {item}")
        sys.exit("Cannot proceed without valid hostnames")

    hosts = valid_hosts
    print(f"✅  Using hosts: {hosts}")

    cfg = {
        "cloudlab": {
            "ssh_user": ssh_user,
            "ssh_key": ssh_key,
            "nodes": hosts,
        },
        "pod_network_cidr": pod_network_cidr,
        "deploy_srearena": deploy_srearena,
        "deploy_key": deploy_key,
    }

    print(f"🔍 Debug: Config: \n{json.dumps(cfg, indent=2)}")

    print("⌛  Waiting (≤20 min) for nodes to get ready …")
    t0 = time.time()
    check_count = 0
    while time.time() - t0 < 1200:  # 20 minutes
        elapsed = time.time() - t0
        check_count += 1

        try:
            if are_nodes_ready(context, slice_name, aggregate_name):
                print(f"✅  All nodes ready after {elapsed:.1f}s!")
                break
        except Exception as e:
            print(f"⚠️  Error checking node reachability (attempt {check_count}): {e}")

        # Print status every minute
        if check_count == 1 or elapsed % 60 < 30:  # First check or every ~minute
            print(f"  Still waiting... {elapsed:.0f}s elapsed, checking {len(hosts)} hosts")

        time.sleep(10)  # Check every 10 seconds
    else:
        print("⚠️  Nodes not reachable after 20 min – skipping K8s bootstrap")
        print("    You can try running the following manually once nodes are ready:")
        print(f"    ssh {ssh_user}@{hosts[0]}")
        return

    print("🚀  Running cluster_setup …")
    try:
        if deploy_srearena:
            setup_cloudlab_cluster_with_srearena(cfg)
        else:
            setup_cloudlab_cluster(cfg)
        print("✅  Kubernetes cluster ready!")
    except Exception as e:
        print(f"❌  Cluster setup failed: {e}")
        print("    Nodes are reachable but Kubernetes setup encountered an error.")


# Define Click command group
@click.group()
def cli():
    """GENI CloudLab Experiment Management Tool"""
    pass


# Create slice command
@cli.command("create-slice")
@click.argument("slice_name")
@click.option("--hours", type=float, default=1, callback=validate_hours, help="Hours until expiration")
@click.option("--description", default="CloudLab experiment", help="Slice description")
def cmd_create_slice(slice_name, hours, description):
    """Create a new slice"""
    context = geni.util.loadContext()
    create_slice(context, slice_name, hours, description)


# Create sliver command
@cli.command("create-sliver")
@click.argument("slice_name")
@click.argument("rspec_file")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    required=True,
    help="CloudLab site",
)
def cmd_create_sliver(slice_name, rspec_file, site):
    """Create a new sliver"""
    context = geni.util.loadContext()
    create_sliver(context, slice_name, rspec_file, site)


# Sliver status command
@cli.command("sliver-status")
@click.argument("slice_name")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    required=True,
    help="CloudLab site",
)
def cmd_sliver_status(slice_name, site):
    """Get sliver status"""
    context = geni.util.loadContext()
    get_sliver_status(context, slice_name, site)


# Renew slice command
@cli.command("renew-slice")
@click.argument("slice_name")
@click.option("--hours", type=float, default=1, callback=validate_hours, help="Hours to extend")
def cmd_renew_slice(slice_name, hours):
    """Renew a slice"""
    context = geni.util.loadContext()
    renew_slice(context, slice_name, hours)


# Renew sliver command
@cli.command("renew-sliver")
@click.argument("slice_name")
@click.option("--hours", type=float, default=1, callback=validate_hours, help="Hours to extend")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    required=True,
    help="CloudLab site",
)
def cmd_renew_sliver(slice_name, hours, site):
    """Renew a sliver"""
    context = geni.util.loadContext()
    renew_sliver(context, slice_name, hours, site)


# List slices command
@cli.command("list-slices")
def cmd_list_slices():
    """List all slices"""
    context = geni.util.loadContext()
    list_slices(context)


# List sliver specifications command
@cli.command("sliver-spec")
@click.argument("slice_name")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    required=True,
    help="CloudLab site",
)
def cmd_sliver_spec(slice_name, site):
    """List sliver specifications"""
    context = geni.util.loadContext()
    list_sliver_spec(context, slice_name, site)


# Delete sliver command
@cli.command("delete-sliver")
@click.argument("slice_name")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    required=True,
    help="CloudLab site",
)
def cmd_delete_sliver(slice_name, site):
    """Delete a sliver"""
    context = geni.util.loadContext()
    delete_sliver(context, slice_name, site)


# Get hardware info command
@cli.command("get-hardware-info")
def cmd_get_hardware_info():
    """Get available hardware information from CloudLab"""
    get_hardware_info()


# Create experiment command
# @cli.command("create-experiment")
# @click.option("--hardware-type", required=True, help="Hardware type for the nodes")
# @click.option("--duration", type=float, default=1, callback=validate_hours, help="Duration in hours")
# @click.option("--node-count", type=int, default=3, help="Number of nodes to create (default: 3)")
# @click.option(
#     "--os-type", type=click.Choice(OS_TYPES), default="UBUNTU22-64-STD", help="OS image (default: UBUNTU22-64-STD)"
# )
# def cmd_create_experiment(hardware_type, duration, node_count, os_type):
#     """Create a 3-node experiment with specified hardware type"""
#     context = geni.util.loadContext()
#     create_experiment(context, hardware_type, duration, node_count, os_type)


# Renew experiment command
@cli.command("renew-experiment")
@click.argument("slice_name")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    required=True,
    help="CloudLab site",
)
@click.option("--hours", type=float, default=1, callback=validate_hours, help="Hours to extend")
def cmd_renew_experiment(slice_name, site, hours):
    """Renew both slice and sliver for an experiment"""
    context = geni.util.loadContext()
    renew_experiment(context, slice_name, site, hours)


# Create experiment command
@cli.command("create-experiment")
@click.option("--hardware-type", default="c220g5", help="Hardware type")
@click.option("--nodes", type=int, default=3, help="Number of nodes")
@click.option("--duration", type=int, default=1, help="Duration in hours")
@click.option("--os-type", default="UBUNTU22-64-STD", help="OS image")
@click.option("--ssh-user", help="SSH username")
@click.option("--ssh-key", help="SSH privatekey file")
@click.option("--k8s", is_flag=True, help="Bootstrap Kubernetes after sliver is ready")
@click.option("--pod-network-cidr", default="192.168.0.0/16", help="Calico pod CIDR (default 192.168.0.0/16)")
@click.option("--deploy-srearena", is_flag=True, help="Deploy SREArena after K8s cluster is ready")
@click.option("--deploy-key", help="Path to SSH deploy key for SREArena private repo")
def cmd_create_experiment(
    hardware_type, nodes, duration, os_type, k8s, ssh_user, ssh_key, pod_network_cidr, deploy_srearena, deploy_key
):
    """Create slice + sliver quickly"""
    context = geni.util.loadContext()
    create_experiment(
        context,
        hardware_type,
        nodes,
        duration,
        os_type,
        k8s,
        ssh_user,
        ssh_key,
        pod_network_cidr,
        deploy_srearena,
        deploy_key,
    )


if __name__ == "__main__":
    cli()
