import time
from locust import HttpUser, task, between
import requests


class TrainTicketUser(HttpUser):
    wait_time = between(1, 2)

    def on_start(self):
        self.client.verify = False
        self._login()

    def _login(self):
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
        else:
            print(f"Login failed: {response.status_code}")
            self.token = ""
            self.user_id = ""
            self.headers = {"Content-Type": "application/json"}

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

    @task(2)
    def get_routes(self):
        """
        Get all available train routes.
        """
        if not getattr(self, "headers", None):
            return

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
