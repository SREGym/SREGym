import random
import json

from srearena.paths import APP_PATH_LIST
from srearena.service.apps import *

class Randomizer:
    def __init__(self, kubectl):
        self.kubectl = kubectl
        self.namespace = None

    def select_app(self, service_paths=[]):
        # Randomly choose an app from service_paths. If service_paths not provided, choose from list of all available apps. Return reference to app.
        if not service_paths:
            service_path = random.choice(APP_PATH_LIST)
        else:
            service_path = random.choice(service_paths)

        with open(service_path, "r") as file:
            app_metadata = json.load(file)
        
        match app_metadata["Name"]:
            case "OpenTelemetry Demo Astronomy Shop":
                app = AstronomyShop()
            case "Flight Ticket":
                app = FlightTicket()
            case "Hotel Reservation":
                app = HotelReservation()
            case "Social Network":
                app = SocialNetwork()
            case "Train Ticket":
                app = TrainTicket()

        self.namespace = app_metadata["Namespace"]

        return app 

    def select_service(self):
        # Queue kubectl for all available services in app, return service name.
        service_list = [svc.metadata.name for svc in self.kubectl.list_services(namespace=self.namespace).items]
        service = random.choice(service_list)
        return service


    
