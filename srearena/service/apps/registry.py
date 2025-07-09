from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.flight_ticket import FlightTicket
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.apps.socialnet import SocialNetwork
# from srearena.service.apps.train_ticket import TrainTicket

class AppRegistry:
    def __init__(self):
        self.APP_REGISTRY = {
            "OpenTelemetry Demo Astronomy Shop": AstronomyShop,
            "Flight Ticket": FlightTicket,
            "Hotel Reservation": HotelReservation,
            "Social Network": SocialNetwork,
            # "Train Ticket": TrainTicket
        }

    def get_app_instance(self, app_name: str):
        if app_name not in self.APP_REGISTRY:
            raise ValueError(f"App name {app_name} not found in registry.")

        return self.APP_REGISTRY.get(app_name)()
    
    def get_app_names(self):
        return list(self.APP_REGISTRY.keys())