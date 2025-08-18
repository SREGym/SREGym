import json

from srearena.paths import *
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.flight_ticket import FlightTicket
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.helm import Helm

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
    
    
    def load_app_agnostic_information(self, app_name: str):
        """ Deploy the given app, try to find the necessary information to inject fault to the app."""
        app_metadata = self.get_app_metadata(app_name)
        if app_metadata.get("Agnostic Info Ready", False):
            print(f"App {app_name} has already loaded agnostic information.")
            return
        
        print(f"Loading agnostic information for app {app_name}...")
        app_metadata["Agnostic Info Ready"] = True
        

if __name__ == "__main__":
    app_registry = AppRegistry()
    for app_name in app_registry.get_app_names():
        app_registry.load_app_agnostic_information(app_name)
        
