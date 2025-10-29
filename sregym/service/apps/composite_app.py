"""A class representing a composite of mulitple applications"""

import json

from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.base import Application


class CompositeApp:
    def __init__(self, apps: list[Application]):
        self.namespace = "Multiple namespaces"
        self.apps = {}
        for app in apps:
            if app.name in self.apps.keys():
                print(f"[CompositeApp] same app name: {app.name}, continue.")
                continue
            self.apps[app.name] = app
        print(f"[CompositeApp] Apps: {self.apps}")
        self.name = "CompositeApp"
        self.app_name = "CompositeApp"
        self.description = f"Composite application containing {len(self.apps)} apps: {', '.join(self.apps.keys())}"

    def deploy(self):
        # FIXME: this can be optimized to parallel deploy later
        for app in self.apps.values():
            print(f"[CompositeApp] Deploying {app.name}...")
            app.deploy()

    def start_workload(self):
        # FIXME: this can be optimized to parallel start later
        for app in self.apps.values():
            print(f"[CompositeApp] Starting workload for {app.name}...")
            app.start_workload()

    def cleanup(self):
        # FIXME: this can be optimized to parallel cleanup later
        for app in self.apps.values():
            print(f"[CompositeApp] Cleaning up {app.name}...")
            app.cleanup()
