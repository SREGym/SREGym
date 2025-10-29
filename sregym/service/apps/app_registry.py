import json

from sregym.paths import *
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.apps.flight_ticket import FlightTicket
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.helm import Helm

# from sregym.service.apps.train_ticket import TrainTicket


class AppRegistry:
    def __init__(self):
        self.APP_REGISTRY = {
            "Astronomy Shop": AstronomyShop,
            # "Flight Ticket": FlightTicket,
            "Hotel Reservation": HotelReservation,
            "Social Network": SocialNetwork,
            # "Train Ticket": TrainTicket
            "Fleet Cast": FleetCast,
            "Blueprint Hotel Reservation": BlueprintHotelReservation
        }

        self.APP_PATH = {
            "Astronomy Shop": ASTRONOMY_SHOP_METADATA,
            # "Flight Ticket": FLIGHT_TICKET_METADATA,
            "Hotel Reservation": HOTEL_RES_METADATA,
            "Social Network": SOCIAL_NETWORK_METADATA,
            # "Train Ticket": TRAIN_TICKET_METADATA
            "Fleet Cast": FLEET_CAST_METADATA,
            "Blueprint Hotel Reservation": BLUEPRINT_HOTEL_RES_METADATA
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
