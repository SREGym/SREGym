import datetime
import json
import random
import warnings

import click
import geni.portal as portal
import geni.util
from geni.aggregate.cloudlab import Clemson, Utah, Wisconsin

warnings.filterwarnings("ignore")

AGGREGATES_MAP = {
    "utah": Utah,
    "clemson": Clemson,
    "wisconsin": Wisconsin,
}

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


def get_aggregate(site):
    return AGGREGATES_MAP.get(site.lower())


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
        print(res.text)
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


def renew_experiment(context, slice_name, site, hours):
    new_slice_expiration = datetime.datetime.now() + datetime.timedelta(hours=(hours + 1))
    new_sliver_expiration = datetime.datetime.now() + datetime.timedelta(hours=hours)
    try:
        print(f"Renewing slice: {slice_name}")
        context.cf.renewSlice(context, slice_name, new_slice_expiration)
        print(f"Slice '{slice_name}' renewed")
    except Exception as e:
        if "Cannot shorten slice lifetime" in str(e):
            print("Slice already has sufficient lifetime")
        else:
            print(f"Error: {e}")
            return

    try:
        aggregate = get_aggregate(site)

        print(f"Renewing sliver: {slice_name}")
        aggregate.renewsliver(context, slice_name, new_sliver_expiration)
        print(f"Sliver '{slice_name}' renewed")

        print(f"Your experiment under slice: {slice_name} is successfully renewed for {hours} hours\n")
    except Exception as e:
        print(f"Error: {e}")


def create_experiment(
    context,
    hardware_type,
    nodes,
    duration,
    os_type,
    site,
):
    aggregate_name = site.lower()
    aggregate = get_aggregate(aggregate_name)
    if aggregate is None:
        print(f"Unknown site: {site}")
        return

    slice_name = f"exp-{random.randint(100000, 999999)}"
    expires = datetime.datetime.now() + datetime.timedelta(hours=duration)

    # Build simple RSpec
    req = portal.context.makeRequestRSpec()
    pcs = []
    for i in range(nodes):
        n = req.RawPC(f"node{i}")
        n.hardware_type = hardware_type
        n.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{os_type}"
        n.routable_control_ip = True
        pcs.append(n)
    req.Link(members=pcs)

    print(f"Creating slice {slice_name} ...")
    context.cf.createSlice(context, slice_name, exp=expires, desc="Quick experiment via genictl")

    print(f"Allocating sliver on {aggregate_name} ...")
    manifest = aggregate.createsliver(context, slice_name, req)

    geni.util.printlogininfo(manifest=manifest)

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
                    "created_at": datetime.datetime.now().isoformat(),
                    "login_info": login_info,
                },
                indent=2,
            )
        )


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def cli():
    """GENI CloudLab Cluster Management Tool"""
    pass


@cli.command("create-slice")
@click.argument("slice_name")
@click.option("--hours", type=float, default=1, callback=validate_hours, help="Hours until expiration")
@click.option("--description", default="CloudLab experiment", help="Slice description")
def cmd_create_slice(slice_name, hours, description):
    """Create a new slice"""
    context = geni.util.loadContext()
    create_slice(context, slice_name, hours, description)


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


@cli.command("renew-slice")
@click.argument("slice_name")
@click.option("--hours", type=float, default=1, callback=validate_hours, help="Hours to extend")
def cmd_renew_slice(slice_name, hours):
    """Renew a slice"""
    context = geni.util.loadContext()
    renew_slice(context, slice_name, hours)


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


@cli.command("list-slices")
def cmd_list_slices():
    """List all slices"""
    context = geni.util.loadContext()
    list_slices(context)


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


@cli.command("create-experiment")
@click.option("--hardware-type", default="c220g5", help="Hardware type")
@click.option("--nodes", type=int, default=3, help="Number of nodes")
@click.option("--duration", type=int, default=1, help="Duration in hours")
@click.option("--os-type", default="UBUNTU22-64-STD", help="OS image")
@click.option(
    "--site",
    type=click.Choice(["utah", "clemson", "wisconsin"], case_sensitive=False),
    default="wisconsin",
    help="CloudLab site",
)
def cmd_create_experiment(hardware_type, nodes, duration, os_type, site):
    """Create slice + sliver on a specified site"""
    context = geni.util.loadContext()
    create_experiment(
        context,
        hardware_type,
        nodes,
        duration,
        os_type,
        site,
    )


if __name__ == "__main__":
    cli()
