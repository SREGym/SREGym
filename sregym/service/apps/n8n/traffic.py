"""Shared helpers of the n8n workload: the vendors and AP approvers, and a small HTTP client.

The workload runs in the conductor process and reaches n8n, payables-portal and
payments-gateway through their NodePorts, like the ERP and users outside the
cluster would. Nothing here is shipped into the cluster.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

APPROVERS_AP = ("h.weber", "a.costa", "m.dubois", "s.patel", "o.lindgren")
VENDORS = (
    ("Nordwind Logistik GmbH", "DE44500105175407324931"),
    ("Atlas Packaging BV", "NL91ABNA0417164300"),
    ("Brightline Freight Ltd", "GB29NWBK60161331926819"),
    ("Cobalt Steelworks SA", "FR1420041010050500013M02606"),
    ("Delta Cold Chain AB", "SE4550000000058398257466"),
    ("Evergreen Pallets Oy", "FI2112345600000785"),
    ("Fjord Marine Supply AS", "NO9386011117947"),
    ("Granite Machining SpA", "IT60X0542811101000000123456"),
    ("Harbor Customs Brokers", "BE68539007547034"),
    ("Iberia Fasteners SL", "ES9121000418450200051332"),
    ("Juniper Electrical Kft", "HU42117730161111101800000000"),
    ("Kestrel Plastics sro", "CZ6508000000192000145399"),
    ("Lumen Industrial Lighting", "AT611904300234573201"),
    ("Meridian Tooling Ltd", "IE29AIBK93115212345678"),
    ("Northgate Chemicals A/S", "DK5000400440116243"),
    ("Orbit Conveyor Systems", "LU280019400644750000"),
    ("Pioneer Labels sp. z o.o.", "PL61109010140000071219812874"),
    ("Quayside Fuel Services", "PT50000201231234567890154"),
)
ERP = "Apache-HttpClient/4.5.14 (Java/17.0.12)"


def http(method, url, body=None, *, form=False, agent=ERP, timeout=60):
    data, headers = None, {"User-Agent": agent, "Accept": "application/json"}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw.decode(errors="replace")
    return status, payload
