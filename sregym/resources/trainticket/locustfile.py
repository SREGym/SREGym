import time

import requests
from locust import HttpUser, between, task


class TrainTicketUser(HttpUser):
    wait_time = between(1, 2)

    def on_start(self):
        self.client.verify = False
        self.last_login_time = 0
        self.login_interval = 1800  # 30 minutes in seconds
        self._login()

    def _login(self):
        current_time = time.time()
        self.last_login_time = current_time

        response = self.client.post(
            "/api/v1/users/login",
            json={"username": "fdse_microservice", "password": "111111"},
            headers={"Content-Type": "application/json"},
            name="/users/login",
        )
        if response.status_code == 200:
            data = response.json()
            self.token = data.get("data", {}).get("token", "")
            self.user_id = data.get("data", {}).get("userId", "")
            self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            print(f"[Login] Successfully logged in at {current_time}")
        else:
            print(f"[Login] Failed: {response.status_code}")
            self.token = ""
            self.user_id = ""
            self.headers = {"Content-Type": "application/json"}

    # The JWT token is valid for 1 hours, so we need to refresh it if it's expired.
    def _check_and_refresh_token(self):
        current_time = time.time()
        if current_time - self.last_login_time > self.login_interval:
            print(f"[Token] Refreshing token after {current_time - self.last_login_time:.0f} seconds")
            self._login()

    def _get_existing_order_id(self):
        if not getattr(self, "user_id", None):
            return None

        payload = {"loginId": self.user_id}

        # Primary: ts-order-service refresh (POST)
        try:
            resp = self.client.post(
                "/api/v1/orderservice/order/refresh",
                json=payload,
                headers=self.headers,
                name="/orders/refresh",
            )
            if resp.status_code == 200:
                data = resp.json()
                orders = data.get("data", [])
                if orders:
                    first = orders[0] if isinstance(orders, list) else orders
                    oid = first.get("id") or first.get("orderId")
                    if oid:
                        return oid
            else:
                print(f"[Orders] Refresh failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"[Orders] Error calling refresh: {e}")
            return None

    @task(1)
    def request_voucher(self):
        """Request a voucher for an existing order via the voucher service."""
        if not getattr(self, "headers", None):
            return

        order_id = self._get_existing_order_id()
        if not order_id:
            return

        payload = {"orderId": order_id, "type": 1}
        start = time.time()

        try:
            with self.client.post(
                "http://ts-voucher-service:16101/getVoucher",
                json=payload,
                headers={"Content-Type": "application/json"},
                name="/getVoucher",
                timeout=5,
                catch_response=True,
            ) as response:
                elapsed = time.time() - start
                print(f"[Voucher] /getVoucher status={response.status_code} elapsed={elapsed:.2f}s")

                if response.status_code == 200:
                    response.success()
                else:
                    response.failure(f"Voucher service returned status {response.status_code}. Elapsed: {elapsed:.2f}s")

        except requests.exceptions.ReadTimeout:
            elapsed = time.time() - start
            print(f"[Voucher] /getVoucher timed out after {elapsed:.2f}s")

        except Exception as e:
            elapsed = time.time() - start
            print(f"[Voucher] /getVoucher error after {elapsed:.2f}s: {e}")

    @task(1)
    def create_contact(self):
        """Create a new contact and clean it up afterwards."""
        if not getattr(self, "headers", None):
            return

        self._check_and_refresh_token()

        import uuid

        unique_name = f"TestContact_{uuid.uuid4().hex[:8]}"

        contact_payload = {
            "name": unique_name,
            "accountId": self.user_id,
            "documentType": 1,
            "documentNumber": unique_name,
            "phoneNumber": f"555-{unique_name[-4:]}",
        }

        print(f"[Contacts] Creating contact: {unique_name}")

        try:
            with self.client.post(
                "/api/v1/contactservice/contacts",
                json=contact_payload,
                headers=self.headers,
                name="/contacts/create",
                catch_response=True,
            ) as response:
                if response.status_code == 201:
                    data = response.json()
                    status = data.get("status", -1)
                    msg = data.get("msg", "")

                    if status == 1:
                        print("[Contacts] Contact created successfully")

                        # Clean up: Delete the contact to avoid crowding the list
                        contact_id = data.get("data", {}).get("id")
                        if contact_id:
                            try:
                                delete_response = self.client.delete(
                                    f"/api/v1/contactservice/contacts/{contact_id}",
                                    headers=self.headers,
                                    name="/contacts/delete",
                                )
                                if delete_response.status_code == 200:
                                    print(f"[Contacts] Cleanup: Contact {contact_id} deleted")
                                else:
                                    print(
                                        f"[Contacts] Cleanup: Failed to delete contact {contact_id}, status: {delete_response.status_code}"
                                    )
                            except Exception as e:
                                print(f"[Contacts] Cleanup: Error deleting contact {contact_id}: {e}")

                        response.success()

                    elif status == 0:
                        print(f"[Contacts] Contact creation failed: {msg}")
                        response.failure(f"Contact creation failed: {msg}")
                    else:
                        print(f"[Contacts] Unexpected status {status}: {msg}")
                        response.failure(f"Contact creation returned unexpected status: {status}")

                else:
                    print(f"[Contacts] HTTP error: {response.status_code}")
                    response.failure(f"Contact creation HTTP error: {response.status_code}")

        except Exception as e:
            print(f"[Contacts] Error during contact creation: {e}")

    @task(2)
    def get_routes(self):
        """Get all available train routes."""
        if not getattr(self, "headers", None):
            return

        self._check_and_refresh_token()

        try:
            response = self.client.get(
                "/api/v1/routeservice/routes",
                headers=self.headers,
                name="/routes/get",
            )

            if response.status_code == 200:
                data = response.json()
                routes_count = len(data.get("data", [])) if isinstance(data.get("data"), list) else 0  # noqa: F841
                print(f"[Routes] Retrieved {routes_count} routes")
            else:
                print(f"[Routes] Failed to get routes: {response.status_code}")

        except Exception as e:
            print(f"[Routes] Error getting routes: {e}")

    @task(2)
    def get_stations(self):
        """Get all available train stations."""
        if not getattr(self, "headers", None):
            return

        self._check_and_refresh_token()

        try:
            response = self.client.get(
                "/api/v1/stationservice/stations",
                headers=self.headers,
                name="/stations/get",
            )

            if response.status_code == 200:
                data = response.json()
                stations_count = len(data.get("data", [])) if isinstance(data.get("data"), list) else 0  # noqa: F841
                print(f"[Stations] Retrieved {stations_count} stations")
            else:
                print(f"[Stations] Failed to get stations: {response.status_code}")

        except Exception as e:
            print(f"[Stations] Error getting stations: {e}")
