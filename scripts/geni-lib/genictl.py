import argparse
import datetime
import json
import random
import warnings

import geni.portal as portal
import geni.util
from geni.aggregate.cloudlab import Clemson, Utah, Wisconsin
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding import KeyBindings

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


def validate_hours(value):
    float_value = float(value)
    if float_value <= 0:
        raise argparse.ArgumentTypeError("Hours must be greater than 0")
    return float_value


def create_slice(context, args):
    try:
        print(f"Creating slice '{args.slice_name}'...")
        expiration = datetime.datetime.now() + datetime.timedelta(hours=args.hours)
        res = context.cf.createSlice(context, args.slice_name, exp=expiration, desc=args.description)
        print(f"Slice Info: \n{json.dumps(res, indent=2)}")
        print(f"Slice '{args.slice_name}' created")
    except Exception as e:
        print(f"Error: {e}")


def create_sliver(context, args):
    try:
        print(f"Creating sliver in slice '{args.slice_name}'...")
        aggregate = get_aggregate(args.site)
        igm = aggregate.createsliver(context, args.slice_name, args.rspec_file)
        geni.util.printlogininfo(manifest=igm)

        # Save the login info to a file
        login_info = geni.util._corelogininfo(igm)
        if isinstance(login_info, list):
            login_info = "\n".join(map(str, login_info))
        with open(f"{args.slice_name}.login.info.txt", "w") as f:
            f.write(f"Slice name: {args.slice_name}\n")
            f.write(f"Cluster name: {aggregate.name}\n")
            f.write(login_info)

        print(f"Sliver '{args.slice_name}' created")
    except Exception as e:
        print(f"Error: {e}")


def get_sliver_status(context, args):
    try:
        print("Checking sliver status...")
        aggregate = get_aggregate(args.site)
        status = aggregate.sliverstatus(context, args.slice_name)
        print(f"Status: {json.dumps(status, indent=2)}")
    except Exception as e:
        print(f"Error: {e}")


def renew_slice(context, args):
    try:
        print("Renewing slice...")
        new_expiration = datetime.datetime.now() + datetime.timedelta(hours=args.hours)
        context.cf.renewSlice(context, args.slice_name, new_expiration)
        print(f"Slice '{args.slice_name}' renewed")
    except Exception as e:
        print(f"Error: {e}")


def renew_sliver(context, args):
    try:
        print("Renewing sliver...")
        aggregate = get_aggregate(args.site)
        new_expiration = datetime.datetime.now() + datetime.timedelta(hours=args.hours)
        aggregate.renewsliver(context, args.slice_name, new_expiration)
        print(f"Sliver '{args.slice_name}' renewed")
    except Exception as e:
        print(f"Error: {e}")


def list_slices(context, args):
    try:
        print("Listing slices...")
        res = context.cf.listSlices(context)
        print(json.dumps(res, indent=2))
    except Exception as e:
        print(f"Error: {e}")


def list_sliver_spec(context, args):
    try:
        print("Listing slivers...")
        aggregate = get_aggregate(args.site)
        res = aggregate.listresources(context, args.slice_name, available=True)

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


def delete_sliver(context, args):
    try:
        print(f"Deleting sliver '{args.slice_name}'...")
        aggregate = get_aggregate(args.site)
        aggregate.deletesliver(context, args.slice_name)
        print(f"Sliver '{args.slice_name}' deleted.")
    except Exception as e:
        print(f"Error: {e}")


def get_aggregate(site):
    sites = {"utah": Utah, "clemson": Clemson, "wisconsin": Wisconsin}
    return sites.get(site.lower(), Utah)


def get_hardware_info(context=None, args=None):
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


def quick_experiment_creation(context, args):
    try:
        hardware_type = args.hardware_type
        duration = args.duration
        node_count = args.node_count if hasattr(args, "node_count") else 3
        os_type = args.os_type if hasattr(args, "os_type") else "UBUNTU22-64-STD"
        os_urn = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{os_type}"

        print(f"Creating a quick {node_count} node cluster of hardware type: {hardware_type}")

        hardware_info_list = collect_and_parse_hardware_info()
        slice_name = "test-" + str(random.randint(100000, 999999))
        cluster_name = None

        for item in hardware_info_list:
            # print(f"Checking {item['hardware_name']} at {item['cluster_name']}")
            if item["hardware_name"].strip() == hardware_type.strip():
                if item["total"] >= node_count and item["free"] >= node_count:
                    print(f"Creating a {node_count} node cluster of {hardware_type} at {item['cluster_name']}")
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

        # Create a cluster of the desired hardware type with specified number of nodes
        request = portal.context.makeRequestRSpec()

        nodes = []
        # Create the control node
        nodes.append(request.RawPC("control"))
        # Create the compute nodes
        for i in range(1, node_count):
            nodes.append(request.RawPC(f"compute{i}"))

        # Set hardware type and OS image for all nodes
        for node in nodes:
            node.hardware_type = hardware_type
            node.disk_image = os_urn

        # Link all nodes together
        link1 = request.Link(members=nodes)

        ### Create the slice
        try:
            print(f"Creating slice: {slice_name}")
            expiration = datetime.datetime.now() + datetime.timedelta(hours=duration)
            ret = context.cf.createSlice(context, slice_name, exp=expiration)
            print(f"Slice created: {slice_name} for {duration} hours\n")
            print(f"Slice Info: {json.dumps(ret, indent=2)}\n")
        except Exception as e:
            print(f"Error creating slice: {e}")
            exit(1)

        ### Create the sliver (actual experiment)
        print(f"Creating sliver in slice: {slice_name}")
        try:
            igm = aggregate.createsliver(context, slice_name, request)
            print(f"Sliver created\n")
        except Exception as e:
            print(f"Error creating sliver: {e}")
            exit(1)

        geni.util.printlogininfo(manifest=igm)

        print("Your ssh info:")
        geni.util.printlogininfo(manifest=igm)

        ### Save the login info to a file
        login_info = geni.util._corelogininfo(igm)
        if isinstance(login_info, list):
            login_info = "\n".join(map(str, login_info))
        with open(f"{slice_name}.login.info.txt", "a") as f:
            f.write(f"Slice name: {slice_name}\n")
            f.write(f"Cluster name: {cluster_name}\n")
            f.write(f"Duration: {duration} hours\n")
            f.write(f"Hardware type: {hardware_type}\n")
            f.write(f"Node count: {node_count}\n")
            f.write(f"OS Image: {os_type}\n")
            f.write("Login info:\n")
            f.write(login_info)
            f.write("\n")
            f.write("To delete the experiment, run the following command:\n")
            f.write(f"python3 genictl.py delete-sliver {slice_name} --site {aggregate_name}\n")
        print(f"\nSSH info saved to {slice_name}.login.info.txt\n")

        print(
            f"Your experiment under slice: {slice_name} is successfully created for {duration} hours at {aggregate_name}\n"
        )

    except Exception as e:
        print(f"Error: {e}")


def main():
    commands = [
        "create-slice",
        "create-sliver",
        "sliver-status",
        "renew-slice",
        "renew-sliver",
        "list-slices",
        "sliver-spec",
        "delete-sliver",
        "get-hardware-info",
        "quick-experiment",
    ]
    sites = ["utah", "clemson", "wisconsin"]

    parser = argparse.ArgumentParser(
        description="GENI CloudLab Experiment Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # create slice parser
    create_slice_parser = subparsers.add_parser("create-slice", help="Create a new slice")
    create_slice_parser.add_argument("slice_name", help="Name of the slice")
    create_slice_parser.add_argument("--hours", type=validate_hours, default=1, help="Hours until expiration")
    create_slice_parser.add_argument("--description", default="CloudLab experiment", help="Slice description")

    # create sliver parser
    create_sliver_parser = subparsers.add_parser("create-sliver", help="Create a new sliver")
    create_sliver_parser.add_argument("slice_name", help="Name of the slice")
    create_sliver_parser.add_argument("rspec_file", help="Path to RSpec file")
    create_sliver_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    # sliver status parser
    status_parser = subparsers.add_parser("sliver-status", help="Get sliver status")
    status_parser.add_argument("slice_name", help="Name of the slice")
    status_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    renew_slice_parser = subparsers.add_parser("renew-slice", help="Renew a slice")
    renew_slice_parser.add_argument("slice_name", help="Name of the slice")
    renew_slice_parser.add_argument("--hours", type=validate_hours, default=1, help="Hours to extend")

    # renew sliver parser
    renew_sliver_parser = subparsers.add_parser("renew-sliver", help="Renew a sliver")
    renew_sliver_parser.add_argument("slice_name", help="Name of the slice")
    renew_sliver_parser.add_argument("--hours", type=validate_hours, default=1, help="Hours to extend")
    renew_sliver_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    # list sliver spec parser
    list_spec_parser = subparsers.add_parser("sliver-spec", help="List sliver specifications")
    list_spec_parser.add_argument("slice_name", help="Name of the slice")
    list_spec_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    # delete sliver parser
    delete_parser = subparsers.add_parser("delete-sliver", help="Delete a sliver")
    delete_parser.add_argument("slice_name", help="Name of the slice")
    delete_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    # list slices parser
    list_slices_parser = subparsers.add_parser("list-slices", help="List all slices")

    # get hardware info parser
    subparsers.add_parser("get-hardware-info", help="Get available hardware information from CloudLab")

    # quick experiment parser
    quick_exp_parser = subparsers.add_parser(
        "quick-experiment",
        help="Create a quick 3-node experiment with specified hardware type",
    )
    quick_exp_parser.add_argument("--hardware-type", required=True, help="Hardware type for the nodes")

    quick_exp_parser.add_argument("--duration", type=validate_hours, default=1, help="Duration in hours")

    quick_exp_parser.add_argument(
        "--node-count",
        type=int,
        default=3,
        help="Number of nodes to create (default: 3)",
    )

    quick_exp_parser.add_argument(
        "--os-type",
        default="UBUNTU22-64-STD",
        choices=OS_TYPES,
        help="OS image (default: UBUNTU22-64-STD)",
    )

    # Add interactive mode flag
    parser.add_argument("--interactive", "-i", action="store_true", help="Run in interactive mode")

    args = parser.parse_args()

    if args.interactive:
        run_interactive_mode(parser, commands, sites)
    else:
        if not args.command:
            parser.print_help()
            return

        context = geni.util.loadContext()
        commands_map = {
            "create-slice": create_slice,
            "create-sliver": create_sliver,
            "sliver-status": get_sliver_status,
            "renew-slice": renew_slice,
            "renew-sliver": renew_sliver,
            "list-slices": list_slices,
            "sliver-spec": list_sliver_spec,
            "delete-sliver": delete_sliver,
            "get-hardware-info": get_hardware_info,
            "quick-experiment": quick_experiment_creation,
        }
        commands_map[args.command](context, args)


def run_interactive_mode(parser, commands, sites):
    command_completer = WordCompleter(commands, ignore_case=True)
    site_completer = WordCompleter(sites, ignore_case=True)
    os_type_completer = WordCompleter(OS_TYPES, ignore_case=True)

    kb = KeyBindings()

    session = PromptSession(
        multiline=False,
        completer=command_completer,
        editing_mode=EditingMode.EMACS,
        complete_while_typing=True,
        key_bindings=kb,
    )

    site_session = PromptSession(
        completer=site_completer,
        editing_mode=EditingMode.EMACS,
        complete_while_typing=True,
        key_bindings=kb,
    )

    os_session = PromptSession(
        completer=os_type_completer,
        editing_mode=EditingMode.EMACS,
        complete_while_typing=True,
        key_bindings=kb,
    )

    parser.print_help()

    while True:
        try:
            command_input = session.prompt("> ")
            if command_input.lower() in ["exit", "q"]:
                break

            if not command_input.strip():
                continue

            if command_input.strip() in ["-h", "--help", "help"]:
                parser.print_help()
                continue

            input_parts = command_input.split()
            args_list = input_parts

            if input_parts[0] == "list-slices":
                args_list = ["list-slices"]
            elif input_parts[0] in [
                "create-sliver",
                "sliver-status",
                "renew-sliver",
                "sliver-spec",
                "delete-sliver",
            ]:
                while True:
                    site = site_session.prompt("Enter site (utah, clemson, wisconsin): ").strip()
                    if site in sites:
                        break
                    print("Error: Please enter a valid site (utah, clemson, or wisconsin)")
                args_list.append("--site")
                args_list.append(site)

            if input_parts[0] in [
                "create-slice",
                "create-sliver",
                "sliver-status",
                "renew-slice",
                "renew-sliver",
                "sliver-spec",
                "delete-sliver",
            ]:
                while True:
                    slice_name = input("Enter slice name: ").strip()
                    if slice_name:
                        break
                    print("Error: Slice name cannot be empty")
                args_list.append(slice_name)

            if input_parts[0] == "create-sliver":
                while True:
                    rspec_file = input("Enter path to RSpec file: ").strip()
                    if rspec_file:
                        break
                    print("Error: RSpec file path cannot be empty")
                args_list.append(rspec_file)

            if input_parts[0] in ["create-slice"]:
                hours = input("Enter expiration time (hours from now, default 1): ").strip() or "1"
                args_list.extend(["--hours", hours])

            if input_parts[0] in ["renew-slice", "renew-sliver"]:
                hours = input("Enter new expiration time (hours from now, default 1): ").strip() or "1"
                args_list.extend(["--hours", hours])

            if input_parts[0] == "create-slice":
                description = (
                    input('Enter slice description (default "CloudLab experiment"): ') or "CloudLab experiment"
                )
                args_list.extend(["--description", description])

            if input_parts[0] == "quick-experiment":
                while True:
                    hardware_type = input("Enter hardware type: ").strip()
                    if hardware_type:
                        break
                    print("Error: Hardware type cannot be empty")
                args_list.extend(["--hardware-type", hardware_type])

                duration = input("Enter duration in hours (default 1): ").strip() or "1"
                args_list.extend(["--duration", duration])

                node_count = input("Enter number of nodes (default 3): ").strip() or "3"
                args_list.extend(["--node-count", node_count])

                print("Available OS types:")
                for os_type in OS_TYPES:
                    print(f"  - {os_type}")
                os_response = (
                    os_session.prompt("Enter OS type (default UBUNTU22-64-STD): ").strip() or "UBUNTU22-64-STD"
                )
                if os_response:
                    args_list.extend(["--os-type", os_response])

            args = parser.parse_args(args_list)
            if not args.command:
                parser.print_help()
                continue

            context = geni.util.loadContext()
            commands_map = {
                "create-slice": create_slice,
                "create-sliver": create_sliver,
                "sliver-status": get_sliver_status,
                "renew-slice": renew_slice,
                "renew-sliver": renew_sliver,
                "list-slices": list_slices,
                "sliver-spec": list_sliver_spec,
                "delete-sliver": delete_sliver,
                "get-hardware-info": get_hardware_info,
                "quick-experiment": quick_experiment_creation,
            }
            commands_map[args.command](context, args)

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    main()
