"""Explicit application identities, factories, and metadata locations."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sregym.paths import (
    ASTRONOMY_SHOP_METADATA,
    BLUEPRINT_HOTEL_RES_METADATA,
    FLEET_CAST_METADATA,
    FLIGHT_TICKET_METADATA,
    HOTEL_RES_METADATA,
    SOCIAL_NETWORK_METADATA,
    TRAIN_TICKET_METADATA,
)
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.base import Application
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.apps.deathstarbench.application import (
    ScaledHotelReservationApplication,
    ScaledSocialNetworkApplication,
)
from sregym.service.apps.fleet_cast import FleetCast
from sregym.service.apps.flight_ticket import FlightTicket
from sregym.service.apps.gitea.application import GiteaApplication
from sregym.service.apps.gitlab.application import GitLabApplication
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.mattermost.application import MattermostApplication
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.apps.stripe.application import StripeApplication
from sregym.service.apps.train_ticket import TrainTicket


class RecoveryChecks(Enum):
    """Application invariants, independent of any Conductor oracle class."""

    ENDPOINTS = "endpoints"
    POSTGRES_RECORDS = "postgres-records"
    GIT_REPOSITORIES = "git-repositories"
    MONGODB_RECORDS = "mongodb-records"


@dataclass(frozen=True)
class AppDefinition:
    app_id: str
    factory: Callable[..., Application]
    metadata_file: Path
    aliases: tuple[str, ...] = ()
    scaled_factory: Callable[..., Application] | None = None
    recovery_checks: RecoveryChecks = RecoveryChecks.ENDPOINTS
    scaled_recovery_checks: RecoveryChecks | None = None
    service_port: int | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", self.app_id):
            raise ValueError(f"Invalid application ID: {self.app_id!r}")

    def create(self, scale_tier: str | None = None) -> Application:
        if scale_tier is None:
            return self.factory()
        if self.scaled_factory is None:
            raise ValueError(f"Scaling is not supported for {self.app_id}")
        return self.scaled_factory(tier=scale_tier)

    def checks(self, scale_tier: str | None = None) -> RecoveryChecks:
        if scale_tier is not None and self.scaled_recovery_checks is not None:
            return self.scaled_recovery_checks
        return self.recovery_checks


APPLICATIONS = (
    AppDefinition("astronomy-shop", AstronomyShop, ASTRONOMY_SHOP_METADATA, ("astronomy_shop",)),
    AppDefinition(
        "hotel-reservation",
        HotelReservation,
        HOTEL_RES_METADATA,
        ("hotel_reservation",),
        scaled_factory=ScaledHotelReservationApplication,
        scaled_recovery_checks=RecoveryChecks.MONGODB_RECORDS,
    ),
    AppDefinition(
        "social-network",
        SocialNetwork,
        SOCIAL_NETWORK_METADATA,
        ("social_network",),
        scaled_factory=ScaledSocialNetworkApplication,
        scaled_recovery_checks=RecoveryChecks.MONGODB_RECORDS,
        service_port=9090,
    ),
    AppDefinition("fleet-cast", FleetCast, FLEET_CAST_METADATA, ("fleet_cast",)),
    AppDefinition(
        "blueprint-hotel-reservation",
        BlueprintHotelReservation,
        BLUEPRINT_HOTEL_RES_METADATA,
        ("blueprint_hotel_reservation",),
    ),
    AppDefinition("train-ticket", TrainTicket, TRAIN_TICKET_METADATA, ("train_ticket",)),
    AppDefinition("flight-ticket", FlightTicket, FLIGHT_TICKET_METADATA, ("flight_ticket",)),
    AppDefinition(
        "gitea",
        GiteaApplication,
        GiteaApplication.metadata_file,
        scaled_factory=GiteaApplication,
        recovery_checks=RecoveryChecks.GIT_REPOSITORIES,
    ),
    AppDefinition(
        "gitlab",
        GitLabApplication,
        GitLabApplication.metadata_file,
        ("gitlab_ce", "gitlab-ce"),
        scaled_factory=GitLabApplication,
        recovery_checks=RecoveryChecks.POSTGRES_RECORDS,
    ),
    AppDefinition(
        "mattermost",
        MattermostApplication,
        MattermostApplication.metadata_file,
        scaled_factory=MattermostApplication,
        recovery_checks=RecoveryChecks.POSTGRES_RECORDS,
    ),
    AppDefinition(
        "stripe",
        StripeApplication,
        StripeApplication.metadata_file,
        ("stripe_marathon", "stripe-marathon"),
        scaled_factory=StripeApplication,
        recovery_checks=RecoveryChecks.POSTGRES_RECORDS,
    ),
)


class AppRegistry:
    def __init__(self, definitions: tuple[AppDefinition, ...] = APPLICATIONS):
        self._definitions: dict[str, AppDefinition] = {}
        self._aliases: dict[str, str] = {}
        for definition in definitions:
            if definition.app_id in self._definitions:
                raise ValueError(f"Duplicate application ID: {definition.app_id}")
            self._definitions[definition.app_id] = definition
            metadata = json.loads(definition.metadata_file.read_text())
            for alias in (definition.app_id, *definition.aliases, metadata["Name"]):
                if alias in self._aliases and self._aliases[alias] != definition.app_id:
                    raise ValueError(f"Duplicate application lookup name: {alias}")
                self._aliases[alias] = definition.app_id

    def get_definition(self, app_name: str) -> AppDefinition:
        try:
            return self._definitions[self._aliases[app_name]]
        except KeyError:
            raise ValueError(f"App name {app_name} not found in registry.") from None

    def get_app_instance(self, app_name: str, *, scale_tier: str | None = None) -> Application:
        return self.get_definition(app_name).create(scale_tier)

    def get_app_names(self) -> list[str]:
        """Keep the human-readable names used by existing discovery callers."""
        return [self.get_app_metadata(app_id)["Name"] for app_id in self._definitions]

    def get_app_ids(self) -> list[str]:
        return list(self._definitions)

    def get_app_config_file(self, app_name: str) -> Path:
        return self.get_definition(app_name).metadata_file

    def get_app_metadata(self, app_name: str) -> dict:
        return json.loads(self.get_app_config_file(app_name).read_text())
