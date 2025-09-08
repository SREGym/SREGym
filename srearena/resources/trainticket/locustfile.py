import time
from locust import HttpUser, task, between
import requests


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
            print(f"[Login] Successfully logged in at {current_time}, token: {self.token[:20]}...")
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
                print(f"Orderservice refresh failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"Error calling orderservice refresh: {e}")
            return None

    @task(1)
    def test_fault_17_voucher_slow(self):
        """Test F-17: slow DB due to nested SELECTs via direct voucher service call.
        Expected:
          - F-17 ON: /getVoucher takes >5s and times out -> failure
          - F-17 OFF: /getVoucher returns quickly (<5s) -> success
        """
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
                name="/getVoucher (F17)",
                timeout=5,
                catch_response=True,
            ) as response:
                elapsed = time.time() - start
                print(f"[F17] /getVoucher status={response.status_code} elapsed={elapsed:.2f}s")

                if response.status_code == 200:
                    print(f"[F17] SUCCESS: Voucher retrieved in {elapsed:.2f}s | response: {response.text}")
                    response.success()
                else:
                    print(f"[F17] FAILURE: Status {response.status_code} in {elapsed:.2f}s")
                    response.failure(f"[F17] Voucher service failed to retrieve voucher. Error: {response.text}. Elapsed: {elapsed:.2f}s")

        except requests.exceptions.ReadTimeout as e:
            # F-17 ON: Voucher service sleeps for 10s, causing >5s timeout
            elapsed = time.time() - start
            print(f"[F17] /getVoucher timed out after {elapsed:.2f}s (F17 ON - expected behavior!): {e}")

        except Exception as e:
            # Other errors
            elapsed = time.time() - start
            print(f"[F17] /getVoucher error after {elapsed:.2f}s: {e}")

    @task(1)
    def test_fault_22_sql_column_missing(self):
        """Test F-22: SQL column missing error in contacts service.
        Expected:
          - F-22 ON: Contact creation fails with SQL column missing error -> status 0
          - F-22 OFF: Contact creation succeeds -> status 1
        """
        if not getattr(self, "headers", None):
            return

        self._check_and_refresh_token()

        import uuid
        unique_name = f"TestContact_{uuid.uuid4().hex[:8]}"
        
        # Create contact payload
        contact_payload = {
            "name": unique_name,
            "accountId": self.user_id,
            "documentType": 1,
            "documentNumber": unique_name,
            "phoneNumber": f"555-{unique_name[-4:]}"
        }

        print(f"[F22] Testing contact creation: {unique_name}")

        try:
            # Create contact
            with self.client.post(
                "/api/v1/contactservice/contacts",
                json=contact_payload,
                headers=self.headers,
                name="/contacts/create (F22)",
                catch_response=True,
            ) as response:

                if response.status_code == 201:
                    data = response.json()
                    status = data.get("status", -1)
                    msg = data.get("msg", "")
                    
                    if status == 1:
                        print(f"[F22] SUCCESS: Contact created successfully | status: {status} | msg: {msg}")
                        print(f"[F22] Contact data: {data.get('data', {})}")
                        
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
                                    print(f"[F22] Cleanup: Contact {contact_id} deleted successfully")
                                else:
                                    print(f"[F22] Cleanup: Failed to delete contact {contact_id}, status: {delete_response.status_code}")
                            except Exception as e:
                                print(f"[F22] Cleanup: Error deleting contact {contact_id}: {e}")
                        
                        response.success()
                        
                    elif status == 0:
                        print(f"[F22] FAILURE: Contact creation failed | status: {status} | msg: {msg}")
                        response.failure(f"[F22] Contact creation failed: {msg}")
                    else:
                        print(f"[F22] UNKNOWN: Unexpected status {status} | msg: {msg}")
                        response.failure(f"[F22] Contact creation returned unexpected status: {status}")
                        
                else:
                    print(f"[F22] HTTP ERROR: Status {response.status_code} | Response: {response.text}")
                    response.failure(f"[F22] Contact creation HTTP error: {response.status_code}")

        except Exception as e:
            print(f"[F22] EXCEPTION: Error during contact creation: {e}")
            # Can't call response.failure() here since response is not in scope
            print(f"[F22] Exception details: {e}")

    @task(2)
    def get_routes(self):
        """
        Get all available train routes.
        """
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
                routes_count = len(data.get("data", [])) if isinstance(data.get("data"), list) else 0
                # print(f"[Routes] Successfully retrieved {routes_count} routes")
            else:
                print(f"[Routes] Failed to get routes: {response.status_code}")

        except Exception as e:
            print(f"[Routes] Error getting routes: {e}")

    @task(2)
    def get_stations(self):
        """
        Get all available train stations.
        """
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
                stations_count = len(data.get("data", [])) if isinstance(data.get("data"), list) else 0
                # print(f"[Stations] Successfully retrieved {stations_count} stations")
            else:
                print(f"[Stations] Failed to get stations: {response.status_code}")

        except Exception as e:
            print(f"[Stations] Error getting stations: {e}")
