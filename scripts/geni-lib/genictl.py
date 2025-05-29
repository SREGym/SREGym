import argparse
import datetime
import json

# suppress warnings
import warnings

import geni.util
from geni.aggregate.cloudlab import Clemson, Utah, Wisconsin
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding import KeyBindings
from utils import parse_sliver_info, collect_hardware_info_from_html

warnings.filterwarnings("ignore")


def validate_hours(value):
    float_value = float(value)
    if float_value <= 0:
        raise argparse.ArgumentTypeError("Hours must be greater than 0")
    return float_value


def create_slice(context, args):
    try:
        print(f"Creating slice '{args.slice_name}'...")
        expiration = datetime.datetime.now() + datetime.timedelta(hours=args.hours)
        res = context.cf.createSlice(
            context, args.slice_name, exp=expiration, desc=args.description
        )
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
    hardware_info_list = collect_hardware_info_from_html()
    if hardware_info_list:
        print(f"\n{'Hardware Name':<20} | {'Cluster Name':<30} | {'Total':<7} | {'Free':<7}")
        print("-" * 100)
        
        for item in hardware_info_list:
            if item['total'] > 0 or item['free'] > 0:
                print(f"{item['hardware_name']:<20} | {item['cluster_name']:<30} | {item['total']:<7} | {item['free']:<7}")
    else:
        print("No hardware information available")

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
    ]
    sites = ["utah", "clemson", "wisconsin"]

    parser = argparse.ArgumentParser(
        description="GENI CloudLab Experiment Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    create_slice_parser = subparsers.add_parser(
        "create-slice", help="Create a new slice"
    )
    create_slice_parser.add_argument("slice_name", help="Name of the slice")
    create_slice_parser.add_argument(
        "--hours", type=validate_hours, default=1, help="Hours until expiration"
    )
    create_slice_parser.add_argument(
        "--description", default="CloudLab experiment", help="Slice description"
    )

    create_sliver_parser = subparsers.add_parser(
        "create-sliver", help="Create a new sliver"
    )
    create_sliver_parser.add_argument("slice_name", help="Name of the slice")
    create_sliver_parser.add_argument("rspec_file", help="Path to RSpec file")
    create_sliver_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

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
    renew_slice_parser.add_argument(
        "--hours", type=validate_hours, default=1, help="Hours to extend"
    )

    renew_sliver_parser = subparsers.add_parser("renew-sliver", help="Renew a sliver")
    renew_sliver_parser.add_argument("slice_name", help="Name of the slice")
    renew_sliver_parser.add_argument(
        "--hours", type=validate_hours, default=1, help="Hours to extend"
    )
    renew_sliver_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    list_spec_parser = subparsers.add_parser(
        "sliver-spec", help="List sliver specifications"
    )
    list_spec_parser.add_argument("slice_name", help="Name of the slice")
    list_spec_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    delete_parser = subparsers.add_parser("delete-sliver", help="Delete a sliver")
    delete_parser.add_argument("slice_name", help="Name of the slice")
    delete_parser.add_argument(
        "--site",
        choices=["utah", "clemson", "wisconsin"],
        required=True,
        help="CloudLab site",
    )

    list_slices_parser = subparsers.add_parser("list-slices", help="List all slices")

    # Add get-hardware-info command
    subparsers.add_parser("get-hardware-info", help="Get available hardware information from CloudLab")

    # Add interactive mode flag
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="Run in interactive mode"
    )

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
        }
        commands_map[args.command](context, args)


def run_interactive_mode(parser, commands, sites):
    command_completer = WordCompleter(commands, ignore_case=True)
    site_completer = WordCompleter(sites, ignore_case=True)

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
                    site = site_session.prompt(
                        "Enter site (utah, clemson, wisconsin): "
                    ).strip()
                    if site in sites:
                        break
                    print(
                        "Error: Please enter a valid site (utah, clemson, or wisconsin)"
                    )
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
                hours = (
                    input("Enter expiration time (hours from now, default 1): ").strip()
                    or "1"
                )
                args_list.extend(["--hours", hours])

            if input_parts[0] in ["renew-slice", "renew-sliver"]:
                hours = (
                    input(
                        "Enter new expiration time (hours from now, default 1): "
                    ).strip()
                    or "1"
                )
                args_list.extend(["--hours", hours])

            if input_parts[0] == "create-slice":
                description = (
                    input('Enter slice description (default "CloudLab experiment"): ')
                    or "CloudLab experiment"
                )
                args_list.extend(["--description", description])

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
                }
            commands_map[args.command](context, args)

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    main()
