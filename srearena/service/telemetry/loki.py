"""Interface to Loki logging service."""

import requests


class Loki:
    def __init__(self, base_url):
        self.base_url = base_url

    def query(self, query, limit=100, time=None, direction="backward"):
        url = f"{self.base_url}/loki/api/v1/query"
        params = {"query": query, "limit": limit, "time": time, "direction": direction}
        response = requests.get(url, params=params)
        return response.json()

    def query_range(
        self,
        query,
        limit=100,
        start=None,
        end=None,
        since=None,
        step=None,
        interval=None,
        direction="backward",
    ):
        url = f"{self.base_url}/loki/api/v1/query_range"
        params = {
            "query": query,
            "limit": limit,
            "start": start,
            "end": end,
            "since": since,
            "step": step,
            "interval": interval,
            "direction": direction,
        }
        response = requests.get(url, params=params)
        return response.json()
