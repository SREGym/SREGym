"""Hotel reads, reservations, and canonical-data preservation for SLO problems."""

import json
import uuid

from sregym.conductor.oracles.application_recovery import ApplicationRecoveryCheck


class HotelReservationRecoveryCheck(ApplicationRecoveryCheck):
    collections = {
        "profiles": ("mongodb-profile", "profile-db", "hotels"),
        "reservations": ("mongodb-reservation", "reservation-db", "reservation"),
    }

    def verify_requests(self, url, records):
        profiles = {str(row["id"]): row for row in records["profiles"]}
        position = {"lat": 37.7749, "lon": -122.4194}
        probes = [("/hotels", {**position, "inDate": "2015-04-09", "outDate": "2015-04-10"})]
        probes += [("/recommendations", {**position, "require": mode}) for mode in ("price", "dis", "rate")]
        for path, params in probes:
            body = json.loads(self.request(url, path, params))
            if not isinstance(body, dict) or body.get("type") != "FeatureCollection" or not body.get("features"):
                raise ValueError(f"{path} did not return hotel results")
            for feature in body["features"]:
                row = profiles.get(str(feature.get("id")))
                properties = feature.get("properties", {})
                if (
                    row is None
                    or properties.get("name") != row["name"]
                    or properties.get("phone_number") != row["phoneNumber"]
                ):
                    raise ValueError(f"{path} returned an incorrect hotel profile")
                coordinates = feature.get("geometry", {}).get("coordinates", [])
                expected = [row["address"]["lon"], row["address"]["lat"]]
                if len(coordinates) != 2 or any(abs(a - b) > 0.001 for a, b in zip(coordinates, expected, strict=True)):
                    raise ValueError(f"{path} returned incorrect hotel coordinates")

        # A fresh customer name ties the HTTP result to a real database write.
        customer = "Visitor " + uuid.uuid4().hex
        credentials = {"username": "Cornell_1", "password": "1" * 10}
        login = json.loads(self.request(url, "/user", credentials))
        if login.get("message") != "Login successfully!":
            raise ValueError("User login failed")
        booking = {
            **credentials,
            "hotelId": "1",
            "customerName": customer,
            "inDate": "2030-01-10",
            "outDate": "2030-01-11",
            "number": 1,
        }
        response = json.loads(self.request(url, "/reservation", booking))
        if response.get("message") != "Reserve successfully!":
            raise ValueError("Reservation request failed")
        stored = self.read_collection("reservations", {"customerName": customer})
        if len(stored) != 1 or stored[0].get("hotelId") != "1" or stored[0].get("number") != 1:
            raise ValueError("Reservation response has no matching persistent record")
