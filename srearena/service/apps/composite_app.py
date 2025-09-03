"""A class representing a composite of mulitple applications"""

import json

from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.base import Application


class CompositeApp:
    def __init__(self, apps: list[Application]):
        self.apps = apps

    def deploy(self):
        # FIXME: this can be optimized to parallel deploy later
        for app in self.apps:
            app.deploy()

    def start_workload(self):
        # FIXME: this can be optimized to parallel start later
        for app in self.apps:
            app.start_workload()

    def cleanup(self):
        # FIXME: this can be optimized to parallel cleanup later
        for app in self.apps:
            app.cleanup()
