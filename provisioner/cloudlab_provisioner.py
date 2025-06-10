import datetime
import json
import random
import warnings

import geni.portal as portal
import geni.util

from provisioner.config.settings import (
    AGGREGATES_MAP,
    CLOUD_LAB_CONTEXT_JSON,
    DELETE_EXPERIMENT_ERRORS,
    PRIORITY_HARDWARE_TYPES,
    DefaultSettings,
)
from provisioner.utils.logger import logger
from provisioner.utils.parser import collect_and_parse_hardware_info, parse_sliver_info

warnings.filterwarnings("ignore", category=UserWarning)


class CloudlabProvisioner:
    def __init__(self):

        # create context.json
        with open("context.json", "w") as f:
            json.dump(CLOUD_LAB_CONTEXT_JSON, f, indent=4)

        context_path = "context.json"
        self.context = geni.util.loadContext(path=context_path)
        self.project = self.context.cf.project
        self.framework = self.context.cf.name
        self.cert_path = self.context.cf.cert
        self.key_path = self.context.cf.key
        self.user_name = self.context.uname
        self.user_urn = list(self.context._users)[0].urn
        self.user_pubkeypath = list(self.context._users)[0]._keys[0]

    def get_aggregate(self, aggregate_name: str):
        return AGGREGATES_MAP[aggregate_name.lower()]

    def get_aggregate_version(self, aggregate_name: str):
        aggregate = self.get_aggregate(aggregate_name)
        return aggregate.getversion(context=self.context)

    def get_all_hardware_info(self, hardware_type: str):
        all_hardware_list = collect_and_parse_hardware_info()
        hardware_list = []
        for hardware in all_hardware_list:
            if hardware["hardware_name"] == hardware_type:
                hardware_list.append(hardware)
        return hardware_list

    def print_all_hardware_info(self):
        hardware_list = collect_and_parse_hardware_info()
        print(f"{'Hardware Name':<20} | {'Cluster Name':<30} | {'Total':<7} | {'Free':<7}")
        print("-" * 100)
        for hardware in hardware_list:
            print(
                f"{hardware['hardware_name']:<20} | {hardware['cluster_name']:<30} | {hardware['total']:<7} | {hardware['free']:<7}"
            )

    def get_hardware_available_aggregate_name(self, hardware_type: str, node_count: int):
        hardware_list = self.get_all_hardware_info(hardware_type)
        aggregate_name = None

        for hardware in hardware_list:
            if hardware["hardware_name"] == hardware_type and hardware["free"] >= node_count:
                aggregate_name = hardware["cluster_name"].lower()
                break

        if not aggregate_name:
            logger.error("Error: Requested hardware is not available")
            return None

        return aggregate_name

    def generate_slice_name(self):
        return f"test-{random.randint(100000, 999999)}"

    def create_slice(self, slice_name: str, duration: float, description: str = "Cloudlab Experiment"):
        try:
            expiration = datetime.datetime.now() + datetime.timedelta(hours=duration)
            res = self.context.cf.createSlice(self.context, slice_name, exp=expiration, desc=description)
            return res
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def create_sliver(self, slice_name: str, rspec_file: str, aggregate_name: str):
        try:
            aggregate = self.get_aggregate(aggregate_name)
            igm = aggregate.createsliver(self.context, slice_name, rspec_file)
            geni.util.printlogininfo(manifest=igm)

            login_info = geni.util._corelogininfo(igm)
            return login_info
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def create_rspec(
        self,
        hardware_type: str = DefaultSettings.DEFAULT_HARDWARE_TYPE,
        os_type: str = DefaultSettings.DEFAULT_OS_TYPE,
        node_count: int = DefaultSettings.DEFAULT_NODE_COUNT,
    ):
        os_url = f"urn:publicid:IDN+emulab.net+image+emulab-ops//{os_type}"

        # geni/portal.py keeps state of previous rspec request so we need to reset it otherwise it will throw MultipleRSpecError
        portal.context._request = None
        rspec = portal.context.makeRequestRSpec()

        nodes = []
        nodes.append(rspec.RawPC("control"))
        for i in range(1, node_count):
            nodes.append(rspec.RawPC(f"compute{i}"))

        for node in nodes:
            node.hardware_type = hardware_type
            node.disk_image = os_url

        link = rspec.Link(members=nodes)

        return rspec

    def create_experiment(
        self,
        slice_name: str = None,
        duration: float = DefaultSettings.UNCLAIMED_CLUSTER_TIMEOUT_HOURS,
        description: str = DefaultSettings.DEFAULT_DESCRIPTION,
        hardware_type: str = DefaultSettings.DEFAULT_HARDWARE_TYPE,
        os_type: str = DefaultSettings.DEFAULT_OS_TYPE,
        node_count: int = DefaultSettings.DEFAULT_NODE_COUNT,
        save_info: bool = True,
    ):
        logger.info(
            f"Creating experiment with duration: {duration}, description: {description}, hardware_type: {hardware_type}, os_type: {os_type}, node_count: {node_count}"
        )
        if not slice_name:
            slice_name = self.generate_slice_name()

        for i in range(10):
            logger.info(f"Creating slice {slice_name}: attempt {i+1}")
            slice_info = self.create_slice(slice_name, duration, description)
            if slice_info:
                logger.info(f"Slice {slice_name} created successfully")
                break

        if not slice_info:
            logger.error("Error: Failed to create slice")
            return None

        logger.info(f"Slice Info: {slice_info}")

        # Move the hardware type to the first position in the priority list if it is not already there
        if hardware_type not in PRIORITY_HARDWARE_TYPES:
            PRIORITY_HARDWARE_TYPES.insert(0, hardware_type)
        else:
            PRIORITY_HARDWARE_TYPES.remove(hardware_type)
            PRIORITY_HARDWARE_TYPES.insert(0, hardware_type)

        for i, hardware_type in enumerate(PRIORITY_HARDWARE_TYPES):
            logger.info(f"Getting hardware available aggregate name for {hardware_type}")
            aggregate_name = self.get_hardware_available_aggregate_name(hardware_type, node_count)

            if not aggregate_name:
                logger.error(f"Error: No hardware available for {hardware_type}")
                continue

            logger.info(f"Found hardware available aggregate name: {aggregate_name}")

            logger.info(f"Creating rspec file for {slice_name} in {aggregate_name}")
            rspec_file = self.create_rspec(hardware_type, os_type, node_count)
            logger.info(f"Created rspec file for {slice_name} in {aggregate_name}")

            logger.info(f"Creating sliver for {slice_name} in {aggregate_name}")
            login_info = self.create_sliver(slice_name, rspec_file, aggregate_name)
            logger.info(f"Created sliver for {slice_name} in {aggregate_name}")

            if login_info:
                logger.info(f"Created sliver for {hardware_type} in {aggregate_name}")
                break

        if not login_info:
            logger.error("Error: Requested hardware is not available")
            return None

        experiment_info = {
            "slice_name": slice_name,
            "aggregate_name": aggregate_name,
            "duration": duration,
            "description": description,
            "hardware_type": hardware_type,
            "os_type": os_type,
            "node_count": node_count,
            "created_at": datetime.datetime.now().isoformat(),
            "login_info": login_info,
        }

        if save_info:
            with open(f"{slice_name}.experiment.info.json", "w") as f:
                json.dump(experiment_info, f, indent=4)

        logger.info(
            f"Experiment Successfully created: {slice_name}, duration: {duration}, description: {description}, hardware_type: {hardware_type}, os_type: {os_type}, node_count: {node_count}"
        )

        return experiment_info

    def renew_experiment(self, slice_name: str, duration: float, aggregate_name: str):
        try:
            logger.info(f"Renewing experiment {slice_name} for {duration} hours")

            # Renew the slice (add 1 hour buffer to ensure slice outlives sliver)
            slice_renewal_success = self.renew_slice(slice_name, duration + 1)
            if not slice_renewal_success:
                logger.error(f"Failed to renew slice {slice_name}")
                return False

            logger.info(f"Successfully renewed slice {slice_name} for {duration} hours")

            # Renew the sliver
            sliver_renewal_success = self.renew_sliver(slice_name, aggregate_name, duration)
            if not sliver_renewal_success:
                logger.error(f"Failed to renew sliver for slice {slice_name}")
                return False

            logger.info(f"Successfully renewed experiment {slice_name} for {duration} hours")
            return True
        except Exception as e:
            logger.error(f"Error renewing experiment: {e}")
            return False

    def delete_experiment(self, slice_name: str, aggregate_name: str):
        try:
            logger.info(f"Deleting experiment {slice_name} in {aggregate_name}")
            aggregate = self.get_aggregate(aggregate_name)
            aggregate.deletesliver(self.context, slice_name)
            logger.info(f"Successfully deleted experiment {slice_name} in {aggregate_name}")
            return True
        except Exception as e:
            logger.error(f"Error: {e}")
            if (
                DELETE_EXPERIMENT_ERRORS[1] in str(e)
                or DELETE_EXPERIMENT_ERRORS[2] in str(e)
                or DELETE_EXPERIMENT_ERRORS[3] in str(e)
            ):
                return True
            return False

    def renew_slice(self, slice_name: str, duration: float):
        try:
            new_expiration = datetime.datetime.now() + datetime.timedelta(hours=duration)
            self.context.cf.renewSlice(self.context, slice_name, new_expiration)
            return True
        except Exception as e:
            logger.error(f"Error: {e}")
            return False

    def renew_sliver(self, slice_name: str, aggregate_name: str, duration: float):
        try:
            aggregate = self.get_aggregate(aggregate_name)
            new_expiration = datetime.datetime.now() + datetime.timedelta(hours=duration)
            aggregate.renewsliver(self.context, slice_name, new_expiration)
            return True
        except Exception as e:
            logger.error(f"Error: {e}")
            return False

    def get_sliver_status(self, slice_name: str, aggregate_name: str):
        try:
            aggregate = self.get_aggregate(aggregate_name)
            sliver_info = aggregate.listresources(self.context, slice_name)
            return sliver_info
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def get_sliver_spec(self, slice_name: str, aggregate_name: str):
        try:
            aggregate = self.get_aggregate(aggregate_name)
            sliver_spec = aggregate.sliverstatus(self.context, slice_name)
            return sliver_spec
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def print_experiment_spec(self, slice_name: str, aggregate_name: str):
        sliver_spec = self.get_sliver_spec(slice_name, aggregate_name)
        parsed_sliver_spec = parse_sliver_info(sliver_spec.text)
        try:
            print("\nExperiment Information:")
            print(f"Description: {parsed_sliver_spec['description']}")
            print(f"Expiration: {parsed_sliver_spec['expiration']}")

            print("\nNodes:")
            for node in parsed_sliver_spec["nodes"]:
                print(f"\nNode: {node['client_id']}")
                print(f"  Hostname: {node['hostname']}")
                print(f"  Public IP: {node['public_ip']}")
                print(f"  Internal IP: {node['internal_ip']}")
                print(f"  Hardware: {node['hardware']}")
                print(f"  OS Image: {node['os_image']}")

            print("\nLocation:")
            print(f"  Country: {parsed_sliver_spec['location']['country']}")
            print(f"  Latitude: {parsed_sliver_spec['location']['latitude']}")
            print(f"  Longitude: {parsed_sliver_spec['location']['longitude']}")
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def list_slices(self):
        try:
            slices = self.context.cf.listSlices(self.context)
            return slices
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def are_nodes_ready(self, slice_name: str, aggregate_name: str) -> bool:
        try:
            aggregate = self.get_aggregate(aggregate_name)
            sliver_status = aggregate.sliverstatus(self.context, slice_name)
            resources = sliver_status.get("geni_resources", [])
            return all(resource.get("pg_status") == "ready" for resource in resources)
        except Exception as e:
            logger.error(f"Error: {e}")
            raise e
