"""Saleor: helpers."""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector

APP = "saleor"


API = "saleor-api"


WORKER = "saleor-worker"


API_SERVICE = "svc-saleor-api"


def _replace_deployment(injector: VirtualizationFaultInjector, service: str, deployment_yaml: dict) -> None:
    """Delete and re-apply ``service`` from ``deployment_yaml`` (the original injectors' replace step)."""
    path = injector._write_yaml_to_file(f"{service}-faulty", deployment_yaml)
    injector.kubectl.exec_command(f"kubectl delete deployment {service} -n {injector.namespace}")
    injector.kubectl.exec_command(f"kubectl apply -f {path} -n {injector.namespace}")


_ORIGINAL_APPS = ("astronomy_shop", "hotel_reservation", "social_network")


POSTGRES = "postgres"


CHANNEL = "default-channel"


DUMMY_GATEWAY = "mirumee.payments.dummy"


def state_file(problem, name: str) -> Path:
    return Path(f"/tmp/sregym-{problem.namespace}-{name}.json")


_CHECKOUT_PROBE = r"""
import json, sys, time, urllib.request, urllib.error
URL = "http://svc-saleor-api:8000/graphql/"
CH = "default-channel"
ADDR = {"firstName": "Probe", "lastName": "Oracle", "streetAddress1": "123 Main St", "city": "New York",
        "postalCode": "10001", "country": "US", "countryArea": "NY"}
req = json.loads(sys.stdin.read() or "{}")
def gql(q, v):
    r = urllib.request.Request(URL, data=json.dumps({"query": q, "variables": v}).encode(),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=req.get("timeout", 30)) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": "HTTP %s: %r" % (e.code, e.read()[:300])}]}
    except Exception as e:
        return {"errors": [{"message": repr(e)}]}
def step(name, body, key):
    errs = body.get("errors") or ((body.get("data") or {}).get(key) or {}).get("errors")
    payload = (body.get("data") or {}).get(key)
    if errs or payload is None:
        print(json.dumps({"ok": False, "step": name, "errors": errs}))
        sys.exit(0)
    return payload
variants = req.get("variants") or []
if not variants:
    d = gql("query($c:String!){products(first:40,channel:$c,filter:{stockAvailability:IN_STOCK,isPublished:true})"
            "{edges{node{variants{id quantityAvailable}}}}}", {"c": CH})
    prods = step("discover", d, "products")
    variants = [v["id"] for e in prods["edges"] for v in (e["node"]["variants"] or [])
                if (v.get("quantityAvailable") or 0) > 0][:1]
    if not variants:
        print(json.dumps({"ok": False, "step": "discover", "errors": "no purchasable variant"}))
        sys.exit(0)
orders = []
for variant in variants:
    c = step("checkoutCreate", gql("mutation($c:String!,$v:ID!){checkoutCreate(input:{channel:$c,"
             "email:\"probe@example.com\",lines:[{quantity:1,variantId:$v}]}){checkout{id isShippingRequired "
             "totalPrice{gross{amount}}} errors{field code message}}}", {"c": CH, "v": variant}), "checkoutCreate")
    cid = c["checkout"]["id"]
    total = c["checkout"]["totalPrice"]["gross"]["amount"]
    if c["checkout"]["isShippingRequired"]:
        a = gql("mutation($id:ID!,$a:AddressInput!){checkoutShippingAddressUpdate(id:$id,shippingAddress:$a)"
                "{checkout{shippingMethods{id price{amount}}} errors{field code message}} "
                "checkoutBillingAddressUpdate(id:$id,billingAddress:$a){errors{field code message}}}",
                {"id": cid, "a": ADDR})
        methods = step("checkoutShippingAddressUpdate", a, "checkoutShippingAddressUpdate")["checkout"]["shippingMethods"]
        step("checkoutBillingAddressUpdate", a, "checkoutBillingAddressUpdate")
        if not methods:
            print(json.dumps({"ok": False, "step": "checkoutShippingAddressUpdate", "errors": "no shipping methods"}))
            sys.exit(0)
        m = min(methods, key=lambda m: m["price"]["amount"])
        total = step("checkoutDeliveryMethodUpdate", gql("mutation($id:ID!,$m:ID!){checkoutDeliveryMethodUpdate("
                     "id:$id,deliveryMethodId:$m){checkout{totalPrice{gross{amount}}} errors{field code message}}}",
                     {"id": cid, "m": m["id"]}), "checkoutDeliveryMethodUpdate")["checkout"]["totalPrice"]["gross"]["amount"]
    else:
        step("checkoutBillingAddressUpdate", gql("mutation($id:ID!,$a:AddressInput!){checkoutBillingAddressUpdate("
             "id:$id,billingAddress:$a){errors{field code message}}}", {"id": cid, "a": ADDR}),
             "checkoutBillingAddressUpdate")
    step("checkoutPaymentCreate", gql("mutation($id:ID!,$amt:PositiveDecimal!){checkoutPaymentCreate(id:$id,"
         "input:{gateway:\"mirumee.payments.dummy\",token:\"fully-charged\",amount:$amt}){payment{id} "
         "errors{field code message}}}", {"id": cid, "amt": str(total)}), "checkoutPaymentCreate")
    o = step("checkoutComplete", gql("mutation($id:ID!){checkoutComplete(id:$id){order{id number} "
             "errors{field code message}}}", {"id": cid}), "checkoutComplete")
    if not (o.get("order") or {}).get("number"):
        print(json.dumps({"ok": False, "step": "checkoutComplete", "errors": "no order"}))
        sys.exit(0)
    orders.append(o["order"]["number"])
print(json.dumps({"ok": True, "orders": orders}))
"""


_ADMIN_GQL = r"""
import json, sys, urllib.request
URL = "http://svc-saleor-api:8000/graphql/"
def gql(q, v, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(URL, data=json.dumps({"query": q, "variables": v}).encode(), headers=h)
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())
login = gql('mutation{tokenCreate(email:"admin@example.com",password:"admin"){token errors{message}}}', {})
token = login["data"]["tokenCreate"]["token"]
req = json.loads(sys.stdin.read())
print(json.dumps(gql(req["query"], req.get("variables") or {}, token)))
"""


def _last_json(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def checkout_probe(problem, variants: list[str] | None = None, timeout: float = 30) -> dict:
    """Run one full guest checkout (per variant) through svc-saleor-api from the toolbox."""
    body = json.dumps({"variants": variants or [], "timeout": timeout})
    budget = timeout * 7 * max(1, len(variants or [])) + 30
    try:
        out = problem.app.toolbox_exec("python3 -c " + shlex.quote(_CHECKOUT_PROBE), input_data=body, timeout=budget)
        return _last_json(out)
    except Exception as exc:
        return {"ok": False, "step": "probe", "errors": str(exc)[-400:]}


def admin_gql(problem, query: str, variables: dict | None = None) -> dict:
    out = problem.app.toolbox_exec(
        "python3 -c " + shlex.quote(_ADMIN_GQL), input_data=json.dumps({"query": query, "variables": variables or {}})
    )
    reply = _last_json(out)
    if reply.get("errors"):
        raise RuntimeError(f"GraphQL errors: {reply['errors']}")
    return reply["data"]


def mutation_ok(data: dict, key: str) -> dict:
    payload = data.get(key) or {}
    if payload.get("errors"):
        raise RuntimeError(f"{key} failed: {payload['errors']}")
    return payload


def require_checkout(problem, oracle, variants: list[str] | None = None) -> dict | None:
    """``None`` when a full guest checkout succeeds, else a failure verdict."""
    result = checkout_probe(problem, variants)
    if not result.get("ok"):
        print(f"❌ Guest checkout fails at {result.get('step')}: {str(result.get('errors'))[:300]}")
        return oracle.fail("checkout_probe_failed", step=result.get("step"), errors=str(result.get("errors"))[:300])
    print(f"✅ Guest checkout completes (orders {result.get('orders')})")
    return None


def expect_checkout_failure(problem, step: str | None = None, variants: list[str] | None = None) -> dict:
    """Confirm the injected fault breaks the checkout path (optionally at ``step``)."""
    deadline = time.monotonic() + 60
    while True:
        result = checkout_probe(problem, variants)
        if not result.get("ok") and (step is None or result.get("step") == step):
            print(f"Checkout now fails at {result.get('step')}: {str(result.get('errors'))[:200]}")
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"fault not confirmed: checkout probe returned {result}")
        time.sleep(3)


def apply_manifest(problem, manifest: dict, namespace: str) -> None:
    path = Path(f"/tmp/sregym-{namespace}-{manifest['kind'].lower()}-{manifest['metadata']['name']}.json")
    path.write_text(json.dumps(manifest))
    problem.kubectl.exec_command_checked(f"kubectl apply -f {path} -n {namespace}")


def ensure_namespace(problem, namespace: str) -> None:
    problem.kubectl.exec_command(f"kubectl get ns {namespace} >/dev/null 2>&1 || kubectl create ns {namespace}")


HIDDEN_NAMESPACE = "khaos"


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


ASYNC_LANE_VALUES = {"saleor": {"worker": {"useDedicatedDbRole": True}}}


ASYNC_LANE_PROFILE = ("lite_saleor_async", {"base": "saleor_async_eval", "soak_cycles": 4})


WORKER_ROLE = "saleor_worker_db"


WORKER_PASSWORD = "agentrepair-worker"


DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
