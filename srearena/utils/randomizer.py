import random
import json

from srearena.paths import APP_PATH_LIST
from srearena.service.apps.registry import AppRegistry

class Randomizer:
    def __init__(self, kubectl):
        self.kubectl = kubectl
        self.namespace = None
        self.apps = AppRegistry()

    def select_app(self, service_paths=[]):
        # Randomly choose an app from service_paths. If service_paths not provided, choose from list of all available apps. Return reference to app.
        if not service_paths:
            service_path = random.choice(APP_PATH_LIST)
        else:
            service_path = random.choice(service_paths)

        with open(service_path, "r") as file:
            app_metadata = json.load(file)
        
        app = self.apps.get_app_instance(app_metadata["Name"])

        self.namespace = app_metadata["Namespace"]

        return app

    def select_service(self):
        # Queue kubectl for all available services in app, return service name.
        service_list = [svc.metadata.name for svc in self.kubectl.list_services(namespace=self.namespace).items]
        service = random.choice(service_list)
        print(f"Random service chosen: {service}")
        return service


    
