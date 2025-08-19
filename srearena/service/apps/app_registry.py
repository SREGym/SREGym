import json
from sys import flags

from srearena.paths import *
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.flight_ticket import FlightTicket
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl

# from srearena.service.apps.train_ticket import TrainTicket


class AppRegistry:
    def __init__(self):
        self.APP_REGISTRY = {
            "Astronomy Shop": AstronomyShop,
            # "Flight Ticket": FlightTicket,
            "Hotel Reservation": HotelReservation,
            "Social Network": SocialNetwork,
            # "Train Ticket": TrainTicket
        }

        self.APP_PATH = {
            "Astronomy Shop": ASTRONOMY_SHOP_METADATA,
            # "Flight Ticket": FLIGHT_TICKET_METADATA,
            "Hotel Reservation": HOTEL_RES_METADATA,
            "Social Network": SOCIAL_NETWORK_METADATA,
            # "Train Ticket": TRAIN_TICKET_METADATA
        }

    def get_app_instance(self, app_name: str):
        if app_name not in self.APP_REGISTRY:
            raise ValueError(f"App name {app_name} not found in registry.")

        return self.APP_REGISTRY.get(app_name)()

    def get_app_names(self):
        return list(self.APP_REGISTRY.keys())

    def get_app_config_file(self, app_name: str):
        if app_name not in self.APP_PATH:
            raise ValueError(f"App name {app_name} not found in registry.")

        return self.APP_PATH.get(app_name)

    def get_app_metadata(self, app_name: str):
        config_file = self.get_app_config_file(app_name)
        with open(config_file, "r") as file:
            metadata = json.load(file)

        return metadata
    
    def dump_app_metadata(self, app_metadata, app_name: str):
        config_file = self.get_app_config_file(app_name)
        with open(config_file, "w") as file:
            json.dump(app_metadata, file, indent=4)
    
    
    def load_app_agnostic_information(self, app_name: str):
        """ Deploy the given app, try to find the necessary information to inject fault to the app."""
        
        # delete and reload the application 
        app_instance = self.get_app_instance(app_name)
        app_instance.delete()
        app_instance.deploy()
        
        app_metadata = self.get_app_metadata(app_name)
        if app_metadata.get("Agnostic Info Ready", False):
            print(f"App {app_name} has already loaded agnostic information.")
            return
        
        print(f"== Loading agnostic information for app {app_name}... == ")
        
        self.kubectl = KubeCtl()
        namespace = app_metadata.get("Namespace")
        if not namespace:
            raise ValueError(f"Namespace not found in app metadata for app {app_name} You should specify it.")
        
        app_metadata["Agnostic Info"] = {}
        
        # arbitrarily find the first deployment in the namespace
        # this is for problem: SPSN, ANEN, 
        if app_metadata.get("Agnostic Info", {}).get("Arbitrary Deployment Name", None) is None:
            self.load_arbitrary_deployment_name(app_metadata, namespace)
            self.dump_app_metadata(app_metadata, app_name)
        
        # find two pods from different deployments has the same image and there entrypoint
        # TODO: Not sure if the ENV is needed to overwrite,
        # this is for WBU
        if app_metadata.get("Agnostic Info", {}).get("For WBU", None) is None or \
           app_metadata.get("Agnostic Info", {}).get("For WBU", {}).get("Ready", False) is False:
            self.load_for_wbu(app_metadata, namespace)
            app_metadata["Agnostic Info"]["For WBU"]["Ready"] = True # Enable crash recovery
            self.dump_app_metadata(app_metadata, app_name)
            
        
        
        app_instance.cleanup() # not sure
        app_metadata["Agnostic Info Ready"] = True
        print(f"== Loading agnostic information for app {app_name} finished. == ")
        
        
        
    def load_arbitrary_deployment_name(self, app_metadata, namespace):
        deployments = self.kubectl.get_deployments(namespace)
        if not deployments:
            raise ValueError(f"No deployments found in namespace {namespace} for app {app_name}. Your app should have at least one deployment.")        
        deployment_name = deployments[0].metadata.name
        app_metadata["Agnostic Info"]["Arbitrary Deployment Name"] = deployment_name
        
    def load_for_wbu(self, app_metadata, namespace):
        deployment_list = self.kubectl.get_deployments(namespace)
        for deployment1 in deployment_list:
            for deployment2 in deployment_list:
                if deployment1.metadata.name != deployment2.metadata.name:
                    container1 = self.find_main_container(deployment1)
                    container2 = self.find_main_container(deployment2)
                    if container1.image == container2.image and container1 and container2:
                        app_metadata["Agnostic Info"]["For WBU"] = {
                            "From Deployment": deployment1.metadata.name,
                            "To Deployment": deployment2.metadata.name,
                            "From Container": container1.name,
                            "To Container": container2.name,
                            "From Entrypoint": container1.command,
                            "To Entrypoint": container2.command,
                        }
                        return

    # helper function to find the main container of a deployment
    # Caution this practice try to be robust but still may introduce uncertainty.
    def find_main_container(self, deployment):
        
        # if one container is the only one, it could be the main container'
        if len(deployment.spec.template.spec.containers) == 1:
            return deployment.spec.template.spec.containers[0]
        
        # if there only one container with the key words, then assume it is the main container
        candidate = None
        only = False
        for container in deployment.spec.template.spec.containers:
            for keyword in ['app', 'main', 'primary', 'application']:
                if keyword in container.name:
                    candidate = container
                    only = True
                    break
        if only:
            return candidate
        
        return None
        


if __name__ == "__main__":
    app_registry = AppRegistry()
    app_registry.load_app_agnostic_information("Astronomy Shop")
        
