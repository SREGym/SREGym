"""SREGym problems ported to Saleor, wave 2 (helper saleor_b).

Saleor serves a Django/GraphQL API (``saleor-api`` behind ``svc-saleor-api``)
and a Celery worker (``saleor-worker``) over PostgreSQL (``postgres`` sts),
Valkey and RabbitMQ. The chart's load generator browses the catalog and runs
full guest checkouts (create -> address -> delivery -> dummy payment ->
complete) against the API. Each port keeps the original fault's mechanism and a
state-based mitigation check, re-targeted at a real Saleor component, and is
wrapped with the load generator health oracle by :func:`ported`.

The Astronomy Shop flagd faults and Train Ticket's F22 have no Saleor
equivalent toggle; their ports are configuration/schema analogues on the same
user path (labelled as such in their root-cause descriptions).
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.fd_exhaustion import FDMitigationOracle
from sregym.conductor.oracles.integer_overflow_primary_key_mitigation import IntegerOverflowPrimaryKeyMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.postgres_lock_mitigation import PostgresLockMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.file_descriptor_exhaustion import FileDescriptorExhaustion
from sregym.conductor.problems.integer_overflow_primary_key_astronomy_shop import IntegerOverflowPrimaryKeyAstronomyShop
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.conductor.problems.postgres_lock_contention_product_catalog import PostgresLockContentionProductCatalog
from sregym.utils.decorators import mark_fault_injected

APP = "saleor"
API = "saleor-api"
WORKER = "saleor-worker"
API_SERVICE = "svc-saleor-api"
POSTGRES = "postgres"
CHANNEL = "default-channel"
DUMMY_GATEWAY = "mirumee.payments.dummy"


# ----------------------------------------------------------------------------- helpers
def state_file(problem, name: str) -> Path:
    return Path(f"/tmp/sregym-{problem.namespace}-{name}.json")


# Full guest checkout through the public GraphQL API, the load generator's
# write path. Reads {"variants": [...]} (optional) on stdin; prints one JSON line.
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

# GraphQL as the populatedb superuser (Saleor's dashboard/admin API).
# Reads {"query", "variables"} on stdin; prints the JSON response.
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


class SaleorFaultStateOracle(MitigationOracle):
    """The generic workload health check, then the port's own fault-state check.

    ``problem.fault_check(oracle)`` returns ``None`` when the injected fault is
    gone (and the user path it broke works again), or a failure verdict built
    with ``oracle.fail``.
    """

    FAILURE_CLASSES = {
        "fault_still_present": FailureClass.AGENT_ERROR,
        "checkout_probe_failed": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result.get("success"):
            return result
        try:
            verdict = self.problem.fault_check(self)
        except Exception as exc:
            print(f"❌ Fault-state check raised: {exc}")
            return self.fail_from_exception(exc)
        return verdict or {"success": True}


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


# ============================================================================= astronomy_shop_payment_service_failure
class PaymentGatewayDisabledSaleor(Problem):
    """Analogue of Astronomy Shop's ``paymentFailure`` flag: the channel's payment gateway is switched off.

    The load generator pays every checkout with Saleor's dummy gateway plugin
    (``mirumee.payments.dummy``). Deactivating that plugin's configuration for
    the storefront channel (an admin ``pluginUpdate``) makes every
    ``checkoutPaymentCreate`` fail with ``NOT_SUPPORTED_GATEWAY``: shoppers can
    browse and fill a cart but every purchase fails at the payment step.
    """

    def __init__(self, app_name: str = APP):
        self.faulty_service = API
        self.channel_id: str | None = None
        ported(
            self,
            app_name,
            component=f"deployment/{API} (payment plugin `{DUMMY_GATEWAY}` configuration, channel `{CHANNEL}`)",
            description=(
                "Configuration analogue of a payment service that fails every charge: in Saleor the payment step "
                f"of checkout is handled in-process by the payment gateway plugin `{DUMMY_GATEWAY}` (the dummy "
                f"gateway the storefront pays with), and that plugin's per-channel configuration for `{CHANNEL}` "
                "was deactivated (`active: false`, stored in `plugins_pluginconfiguration`, settable with the "
                "`pluginUpdate` admin mutation). Every `checkoutPaymentCreate` on the channel is rejected with "
                f"`NOT_SUPPORTED_GATEWAY` (`The gateway {DUMMY_GATEWAY} is not available for this checkout`), so "
                "guest checkouts fail at the payment step while browsing, cart creation, addresses and delivery "
                f"keep working and every pod of `{API}` stays Running and Ready. Fix: re-activate the gateway "
                f"plugin for `{CHANNEL}`."
            ),
            oracle_factory=SaleorFaultStateOracle,
        )

    def _channel_id(self) -> str:
        if not self.channel_id:
            data = admin_gql(self, "query($s:String!){channel(slug:$s){id}}", {"s": CHANNEL})
            self.channel_id = data["channel"]["id"]
        return self.channel_id

    def _set_active(self, active: bool) -> None:
        data = admin_gql(
            self,
            "mutation($id:ID!,$ch:ID!,$a:Boolean!){pluginUpdate(id:$id,channelId:$ch,input:{active:$a})"
            "{plugin{id} errors{field message}}}",
            {"id": DUMMY_GATEWAY, "ch": self._channel_id(), "a": active},
        )
        mutation_ok(data, "pluginUpdate")

    def _gateway_active(self) -> bool | None:
        data = admin_gql(
            self, "query($id:ID!){plugin(id:$id){channelConfigurations{active channel{slug}}}}", {"id": DUMMY_GATEWAY}
        )
        for conf in (data.get("plugin") or {}).get("channelConfigurations") or []:
            if conf["channel"]["slug"] == CHANNEL:
                return bool(conf["active"])
        return None

    @mark_fault_injected
    def inject_fault(self):
        self._set_active(False)
        print(f"Deactivated payment plugin {DUMMY_GATEWAY} for channel {CHANNEL}")
        expect_checkout_failure(self, "checkoutPaymentCreate")

    @mark_fault_injected
    def recover_fault(self):
        self._set_active(True)
        print(f"Re-activated payment plugin {DUMMY_GATEWAY} for channel {CHANNEL}")

    def fault_check(self, oracle) -> dict | None:
        active = self._gateway_active()
        if not active:
            print(f"❌ Payment plugin {DUMMY_GATEWAY} is not active for {CHANNEL} (active={active})")
            return oracle.fail("fault_still_present", plugin=DUMMY_GATEWAY, channel=CHANNEL, active=active)
        return require_checkout(self, oracle)


# ============================================================================= astronomy_shop_cart_service_failure
class ChannelDeactivatedSaleor(Problem):
    """Analogue of Astronomy Shop's ``cartFailure`` flag: the storefront channel is deactivated.

    Saleor scopes carts (checkouts), prices and product visibility to a sales
    channel. With ``default-channel`` inactive, ``checkoutCreate`` is rejected
    and anonymous catalog queries on the channel return no products, so no
    shopper can put anything in a cart.
    """

    def __init__(self, app_name: str = APP):
        self.faulty_service = API
        ported(
            self,
            app_name,
            component=f"deployment/{API} (channel `{CHANNEL}`)",
            description=(
                "Configuration analogue of a cart service that errors on every cart operation: Saleor's carts "
                f"(checkouts) are scoped to a sales channel, and the storefront channel `{CHANNEL}` was deactivated "
                "(`channel_channel.is_active = false`, the `channelDeactivate` admin mutation). Every "
                "`checkoutCreate` on the channel is rejected, so no cart can be created and every checkout fails "
                "at its first step; anonymous catalog queries on the inactive channel also return no products. "
                f"The `{API}` pods, the database and the other components stay healthy. Fix: activate the channel "
                "again (`channelActivate`)."
            ),
            oracle_factory=SaleorFaultStateOracle,
        )

    def _channel(self) -> dict:
        return admin_gql(self, "query($s:String!){channel(slug:$s){id isActive}}", {"s": CHANNEL})["channel"]

    @mark_fault_injected
    def inject_fault(self):
        channel = self._channel()
        data = admin_gql(
            self, "mutation($id:ID!){channelDeactivate(id:$id){channel{isActive} errors{message}}}", {"id": channel["id"]}
        )
        mutation_ok(data, "channelDeactivate")
        print(f"Deactivated channel {CHANNEL}")
        expect_checkout_failure(self)

    @mark_fault_injected
    def recover_fault(self):
        channel = self._channel()
        data = admin_gql(
            self, "mutation($id:ID!){channelActivate(id:$id){channel{isActive} errors{message}}}", {"id": channel["id"]}
        )
        mutation_ok(data, "channelActivate")
        print(f"Activated channel {CHANNEL}")

    def fault_check(self, oracle) -> dict | None:
        if not self._channel()["isActive"]:
            print(f"❌ Channel {CHANNEL} is still inactive")
            return oracle.fail("fault_still_present", channel=CHANNEL)
        return require_checkout(self, oracle)


# ============================================================================= astronomy_shop_product_catalog_service_failure
class CatalogUnpublishedSaleor(Problem):
    """Analogue of Astronomy Shop's ``productCatalogFailure`` flag: part of the catalog is unpublished.

    The original made the catalog fail ``GetProduct`` for one product. Here the
    channel listings of the products of two categories (``Sneakers`` and
    ``T-shirts``, 11 products, about two thirds of the purchasable variants the
    load generator buys) are unpublished: they vanish from the storefront and every checkout of one of
    their variants fails at ``checkoutCreate``, while the rest of the catalog
    keeps working. One product alone would be ~2% of the load generator's
    traffic, below what its error rate can tell from noise.
    """

    CATEGORIES = ("sneakers", "t-shirts")

    def __init__(self, app_name: str = APP):
        self.faulty_service = API
        self.products: list[dict] | None = None
        ported(
            self,
            app_name,
            component=f"deployment/{API} (product channel listings, categories `{'`, `'.join(self.CATEGORIES)}`, channel `{CHANNEL}`)",
            description=(
                "Catalog-data analogue of a product catalog service that fails for specific products: the "
                "channel listings (`product_productchannellisting.is_published`) of every product in the "
                f"`{'` and `'.join(self.CATEGORIES)}` categories were unpublished in channel `{CHANNEL}`. Those products disappear from "
                "the storefront (product queries return null for them) and every checkout that adds one of their "
                "variants is rejected by `checkoutCreate`, so a large share of purchases fails while other "
                "products and every pod stay healthy. Fix: publish the products in the channel again "
                "(`productChannelListingUpdate` with `isPublished: true`)."
            ),
            oracle_factory=SaleorFaultStateOracle,
        )

    def _category_products(self) -> list[dict]:
        return [p for category in self.CATEGORIES for p in self._products_of(category)]

    def _products_of(self, category: str) -> list[dict]:
        data = admin_gql(
            self,
            "query($s:String!,$c:String!){category(slug:$s){products(first:50,channel:$c){edges{node{id name "
            "variants{id} channelListings{channel{id slug} isPublished}}}}}}",
            {"s": category, "c": CHANNEL},
        )
        products = []
        for edge in data["category"]["products"]["edges"]:
            node = edge["node"]
            listing = next((cl for cl in node["channelListings"] or [] if cl["channel"]["slug"] == CHANNEL), None)
            if listing is None:
                continue
            products.append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "channel_id": listing["channel"]["id"],
                    "variants": [v["id"] for v in node["variants"] or []],
                    "published": listing["isPublished"],
                }
            )
        return products

    def _load(self) -> list[dict]:
        if self.products is None:
            self.products = json.loads(state_file(self, "catalog-unpublished").read_text())
        return self.products

    def _publish(self, product: dict, published: bool) -> None:
        data = admin_gql(
            self,
            "mutation($id:ID!,$ch:ID!,$p:Boolean!){productChannelListingUpdate(id:$id,input:{updateChannels:"
            "[{channelId:$ch,isPublished:$p}]}){errors{field message}}}",
            {"id": product["id"], "ch": product["channel_id"], "p": published},
        )
        mutation_ok(data, "productChannelListingUpdate")

    @mark_fault_injected
    def inject_fault(self):
        products = [p for p in self._category_products() if p["published"] and p["variants"]]
        if not products:
            raise RuntimeError(f"no published product in categories {self.CATEGORIES}")
        self.products = products
        state_file(self, "catalog-unpublished").write_text(json.dumps(products))
        for product in products:
            self._publish(product, False)
        print(f"Unpublished {[p['name'] for p in products]} in {CHANNEL}")
        expect_checkout_failure(self, "checkoutCreate", variants=[products[0]["variants"][0]])

    @mark_fault_injected
    def recover_fault(self):
        for product in self._load():
            self._publish(product, True)
        print(f"Published {[p['name'] for p in self._load()]} in {CHANNEL} again")

    def fault_check(self, oracle) -> dict | None:
        saved = {p["id"] for p in self._load()}
        current = {p["id"]: p for p in self._category_products()}
        unpublished = sorted(current[i]["name"] for i in saved if i in current and not current[i]["published"])
        missing = sorted(i for i in saved if i not in current)
        if unpublished or missing:
            print(f"❌ Products still unpublished in {CHANNEL}: {unpublished} (missing listings: {missing})")
            return oracle.fail("fault_still_present", unpublished=unpublished, missing=missing)
        return require_checkout(self, oracle, variants=[p["variants"][0] for p in self._load()][:2])


# ============================================================================= trainticket_f22_sql_column_name_mismatch_error
class SchemaColumnDriftSaleor(Problem):
    """Schema-drift analogue of Train Ticket F22 (SQL references a wrong column name).

    F22 shipped contacts-service code whose SQL names a column the table does
    not have. Saleor's code cannot be changed in place, so the port creates the
    same mismatch from the other side: a column the ORM queries on every
    address read and write (``account_address.postal_code``, the address book
    behind checkout shipping/billing addresses) is renamed. Every statement
    that touches addresses fails at execution with ``column ... does not
    exist``, so checkouts fail at the address step while browsing works.
    """

    TABLE = "account_address"
    COLUMN = "postal_code"
    DRIFTED = "postcode"

    def __init__(self, app_name: str = APP):
        self.faulty_service = API
        ported(
            self,
            app_name,
            component=f"statefulset/{POSTGRES} (table `{self.TABLE}`)",
            description=(
                "Schema-drift analogue of a SQL column-name mismatch: the column "
                f"`{self.TABLE}.{self.COLUMN}` in Saleor's PostgreSQL database (`{POSTGRES}`, database `saleor`) "
                f"was renamed to `{self.DRIFTED}`, while the application (Django ORM in `{API}` and "
                f"`{WORKER}`) still queries `{self.COLUMN}`. Every SQL statement on addresses fails at execution "
                f"time with `column {self.TABLE}.{self.COLUMN} does not exist`, so the checkout shipping/billing "
                "address mutations (and anything reading addresses) return errors and guest checkouts fail, "
                "while catalog reads and all pods stay healthy. Fix: rename the column back "
                f"(`ALTER TABLE {self.TABLE} RENAME COLUMN {self.DRIFTED} TO {self.COLUMN}`), keeping its data."
            ),
            oracle_factory=SaleorFaultStateOracle,
        )

    def _columns(self) -> set[str]:
        rows = self.app.psql(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{self.TABLE}'"
        )
        return {line.strip() for line in rows.splitlines() if line.strip()}

    def _address_count(self) -> int:
        return int(self.app.psql(f"SELECT count(*) FROM {self.TABLE}").strip() or 0)

    @mark_fault_injected
    def inject_fault(self):
        state_file(self, "schema-drift").write_text(json.dumps({"addresses": self._address_count()}))
        self.app.psql(f"ALTER TABLE {self.TABLE} RENAME COLUMN {self.COLUMN} TO {self.DRIFTED}")
        print(f"Renamed {self.TABLE}.{self.COLUMN} -> {self.DRIFTED}")
        expect_checkout_failure(self)

    @mark_fault_injected
    def recover_fault(self):
        if self.COLUMN not in self._columns():
            self.app.psql(f"ALTER TABLE {self.TABLE} RENAME COLUMN {self.DRIFTED} TO {self.COLUMN}")
        print(f"Renamed {self.TABLE}.{self.DRIFTED} back to {self.COLUMN}")

    def fault_check(self, oracle) -> dict | None:
        if self.COLUMN not in self._columns():
            print(f"❌ {self.TABLE}.{self.COLUMN} is missing")
            return oracle.fail("fault_still_present", table=self.TABLE, column=self.COLUMN)
        path = state_file(self, "schema-drift")
        before = json.loads(path.read_text()).get("addresses", 0) if path.exists() else 0
        after = self._address_count()
        if after < before:
            print(f"❌ Addresses were lost ({before} -> {after})")
            return oracle.fail("fault_still_present", detail="address rows lost", before=before, after=after)
        return require_checkout(self, oracle)


def apply_manifest(problem, manifest: dict, namespace: str) -> None:
    path = Path(f"/tmp/sregym-{namespace}-{manifest['kind'].lower()}-{manifest['metadata']['name']}.json")
    path.write_text(json.dumps(manifest))
    problem.kubectl.exec_command_checked(f"kubectl apply -f {path} -n {namespace}")


def ensure_namespace(problem, namespace: str) -> None:
    problem.kubectl.exec_command(f"kubectl get ns {namespace} >/dev/null 2>&1 || kubectl create ns {namespace}")


# Helpers that hide the harness's own pods from the agent (as the originals did).
HIDDEN_NAMESPACE = "khaos"


# ============================================================================= integer_overflow_primary_key_astronomy_shop
class IntegerOverflowPrimaryKeySaleor(IntegerOverflowPrimaryKeyAstronomyShop):
    """``payment_payment.id`` (int4 identity) exhausted: every checkout payment fails.

    The original pinned ``reviews.productreviews``' int4 identity sequence at
    2^31-1. Saleor's ``payment_payment`` has the same shape (``integer``
    ``GENERATED BY DEFAULT AS IDENTITY``) and gets one row per checkout
    (``checkoutPaymentCreate``), so pinning its sequence makes every purchase
    fail at the payment step. The mitigation oracle is the original's,
    unchanged: it calls the problem's helpers below, which this class
    re-targets at the payment table, the PostgreSQL StatefulSet and the
    application role ``saleor_app``.
    """

    TABLE = "payment_payment"
    # Immutable columns hashed for the "original rows intact" check.
    SIGNATURE_COLUMNS = ("gateway", "token", "currency", "total", "created_at")
    # The disguised marker payment: a copy of a real payment with this PSP reference.
    SENTINEL_PSP = "pi_3PqLrS2eZvKYlo2C1x8Kq7bN"

    def __init__(self, app_name: str = APP):
        self.problem_id = "integer_overflow_primary_key_saleor"
        self.faulty_service = [API]
        self._baseline_review_ids = None
        self._baseline_review_sig = None
        ported(
            self,
            app_name,
            component=f"table.public.{self.TABLE}@{POSTGRES}",
            description=(
                f"The identity sequence backing `{self.TABLE}.id` (an INTEGER/int4 column, sequence "
                "`payment_paymentmethod_id_seq`) in Saleor's PostgreSQL database (`postgres` StatefulSet, database "
                "`saleor`) has reached its maximum value 2147483647. Saleor inserts one payment row per checkout "
                "(`checkoutPaymentCreate`), so every new payment INSERT fails when nextval() overflows with "
                "`nextval: reached maximum value of sequence`, and every guest checkout fails at the payment step, "
                "while reads of existing payments and orders stay healthy and all pods remain Running. The durable "
                f"fix is a schema migration widening `{self.TABLE}.id` and its sequence to BIGINT (and the "
                "referencing `payment_transaction.payment_id`), keeping existing rows and the primary key."
            ),
            oracle_factory=IntegerOverflowPrimaryKeyMitigationOracle,
        )

    # ------------------------------------------------------------------ SQL plumbing
    def _run_sql(self, query: str) -> None:
        self.app.psql(query)

    def _psql_super(self, query: str, tuples_only: bool = False) -> str:
        del tuples_only  # app.psql is always unaligned, tuples-only
        try:
            return self.app.psql(query)
        except Exception as exc:
            return str(exc)

    def _namespace_exists(self) -> bool:
        return self.namespace in self.kubectl.exec_command(
            f"kubectl get namespace {self.namespace} --no-headers --ignore-not-found"
        )

    def _insert_columns(self) -> list[str]:
        rows = self.app.psql(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
            f"AND table_name='{self.TABLE}' AND column_name <> 'id' ORDER BY ordinal_position"
        )
        return [line.strip() for line in rows.splitlines() if line.strip()]

    def _copy_row_sql(self, overrides: dict[str, str]) -> str:
        columns = self._insert_columns()
        values = [overrides.get(column, column) for column in columns]
        return (
            f"INSERT INTO {self.TABLE} ({', '.join(columns)}) SELECT {', '.join(values)} FROM {self.TABLE} "
            "ORDER BY id DESC LIMIT 1"
        )

    # ------------------------------------------------------------------ helpers the original oracle calls
    def _review_write_status(self) -> str:
        """Insert a payment as the application role in a rolled-back transaction."""
        sql = f"BEGIN; {self._copy_row_sql({'psp_reference': quote_literal('app-write-probe')})}; ROLLBACK;"
        try:
            self.app.psql(sql, user=self.app.APP_ROLE, password=self.app.APP_PASSWORD)
            return "ok"
        except Exception as exc:
            out = str(exc).lower()
        if any(
            m in out
            for m in ("reached maximum value of sequence", "reached minimum value of sequence", "integer out of range")
        ):
            return "exhausted"
        if "permission denied" in out:
            return "denied"
        if "duplicate key" in out or "unique constraint" in out:
            return "collision"
        print(f"payment write probe failed: {out[-300:]}")
        return "other"

    def _review_reads_work(self) -> bool:
        """The payment path end to end: a full guest checkout through the API."""
        result = checkout_probe(self)
        if not result.get("ok"):
            print(f"❌ Guest checkout fails at {result.get('step')}: {str(result.get('errors'))[:300]}")
        return bool(result.get("ok"))

    def _insert_sentinel_row(self) -> None:
        self._run_sql(self._copy_row_sql({"psp_reference": quote_literal(self.SENTINEL_PSP)}))

    def _review_sentinel_present(self) -> bool:
        out = self._psql_super(f"SELECT count(*) FROM {self.TABLE} WHERE psp_reference = '{self.SENTINEL_PSP}'")
        try:
            return int(out.strip().splitlines()[-1]) >= 1
        except (ValueError, IndexError):
            return False

    def _review_row_count(self) -> int:
        out = self._psql_super(f"SELECT count(*) FROM {self.TABLE}")
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    def _id_column_type(self) -> str:
        out = self._psql_super(
            "SELECT data_type FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{self.TABLE}' AND column_name='id'"
        ).strip()
        return out.splitlines()[-1].strip() if out else ""

    def _resolve_id_sequence(self) -> str | None:
        out = self._psql_super(f"SELECT pg_get_serial_sequence('public.{self.TABLE}', 'id')").strip()
        name = out.splitlines()[-1].strip() if out else ""
        return name if name and " " not in name else None

    def _id_sequence_capacity(self) -> dict | None:
        seq = self._resolve_id_sequence()
        if seq is None:
            return None
        out = self._psql_super(
            "SELECT s.seqincrement, s.seqmax, s.seqmin, s.seqcycle, "
            f"(SELECT last_value FROM {seq}), COALESCE((SELECT max(id) FROM {self.TABLE}), 0), "
            f"COALESCE((SELECT min(id) FROM {self.TABLE}), 0), (SELECT is_called FROM {seq}) "
            f"FROM pg_sequence s WHERE s.seqrelid = '{seq}'::regclass"
        ).strip()
        try:
            fields = out.splitlines()[-1].split("|")
            increment, seqmax, seqmin = int(fields[0]), int(fields[1]), int(fields[2])
            cycle = fields[3] == "t"
            last_value, max_id, min_id = int(fields[4]), int(fields[5]), int(fields[6])
            is_called = fields[7] == "t"
        except (ValueError, IndexError):
            return None
        bounds = {
            "smallint": (-32768, 32767),
            "integer": (-2147483648, 2147483647),
            "bigint": (-9223372036854775808, 9223372036854775807),
        }
        col_min, col_max = bounds.get(self._id_column_type(), (seqmin, seqmax))
        if increment == 0:
            return None
        next_value = last_value + increment if is_called else last_value
        if increment > 0:
            headroom = max(0, (seqmax - next_value) // increment + 1)
            collision_free = next_value > max_id
            fits_column = col_min <= next_value and seqmax <= col_max
        else:
            headroom = max(0, (next_value - seqmin) // -increment + 1)
            collision_free = next_value < min_id
            fits_column = seqmin >= col_min and next_value <= col_max
        return {"headroom": headroom, "collision_free": collision_free, "cycle": cycle, "fits_column": fits_column}

    def _id_uniqueness_enforced(self) -> bool:
        out = self._psql_super(
            "SELECT count(*) FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'id' "
            f"WHERE i.indrelid = 'public.{self.TABLE}'::regclass AND i.indisunique AND i.indisvalid "
            "AND i.indisready AND i.indislive AND i.indpred IS NULL AND i.indexprs IS NULL "
            "AND i.indnkeyatts = 1 AND i.indkey[0] = a.attnum AND a.attnotnull"
        )
        try:
            return int(out.strip().splitlines()[-1]) >= 1
        except (ValueError, IndexError):
            return False

    def _review_data_signature(self, ids: list[int]) -> str:
        if not ids:
            return ""
        fields = " || '|' || ".join(f"COALESCE({c}::text, '')" for c in ("id", *self.SIGNATURE_COLUMNS))
        out = self._psql_super(
            f"SELECT md5(COALESCE(string_agg({fields}, ',' ORDER BY id), '')) FROM {self.TABLE} "
            f"WHERE id IN ({','.join(str(i) for i in ids)})"
        ).strip()
        return out.splitlines()[-1].strip() if out else ""

    def _capture_review_baseline(self) -> tuple[list[int], str]:
        out = self._psql_super(f"SELECT id FROM {self.TABLE} ORDER BY id")
        ids = [int(line.strip()) for line in out.splitlines() if line.strip().lstrip("-").isdigit()]
        return ids, self._review_data_signature(ids)

    def _original_reviews_intact(self) -> tuple[bool, str]:
        if not self._baseline_review_ids:
            saved = state_file(self, "int-overflow")
            if saved.exists():
                data = json.loads(saved.read_text())
                self._baseline_review_ids, self._baseline_review_sig = data["ids"], data["sig"]
        if not self._baseline_review_ids:
            return True, "no baseline captured"
        expected = len(self._baseline_review_ids)
        out = self._psql_super(
            f"SELECT count(*) FROM {self.TABLE} WHERE id IN ({','.join(str(i) for i in self._baseline_review_ids)})"
        ).strip()
        try:
            present = int(out.splitlines()[-1])
        except (ValueError, IndexError):
            return False, "could not read the payments table"
        if present != expected:
            return False, f"{expected - present} of {expected} original payments are gone"
        if self._review_data_signature(self._baseline_review_ids) != self._baseline_review_sig:
            return False, "original payment contents were modified"
        return True, "intact"

    # ------------------------------------------------------------------ lifecycle
    @mark_fault_injected
    def inject_fault(self) -> bool:
        self._baseline_review_ids, self._baseline_review_sig = self._capture_review_baseline()
        state_file(self, "int-overflow").write_text(
            json.dumps({"ids": self._baseline_review_ids, "sig": self._baseline_review_sig})
        )
        self._insert_sentinel_row()
        seq = self._resolve_id_sequence()
        if not seq:
            raise RuntimeError(f"no identity sequence behind {self.TABLE}.id")
        self._run_sql(f"SELECT setval('{seq}', {self.INT4_MAX}, true)")
        status = self._review_write_status()
        if status != "exhausted":
            raise RuntimeError(f"sequence exhaustion not confirmed (write status={status})")
        print(f"Pinned {seq} at {self.INT4_MAX}; payment inserts now overflow")
        expect_checkout_failure(self, "checkoutPaymentCreate")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        seq = self._resolve_id_sequence()
        self._run_sql(
            f"ALTER TABLE {self.TABLE} ALTER COLUMN id TYPE bigint; "
            f"ALTER SEQUENCE {seq} AS bigint MAXVALUE 9223372036854775807; "
            "ALTER TABLE payment_transaction ALTER COLUMN payment_id TYPE bigint;"
        )
        status = self._review_write_status()
        if status != "ok":
            raise RuntimeError(f"writes not restored after migration (status={status})")
        print(f"Widened {self.TABLE}.id, {seq} and payment_transaction.payment_id to bigint")
        return True


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ============================================================================= postgres_lock_contention_product_catalog
class SaleorCatalogLockMitigationOracle(PostgresLockMitigationOracle):
    """``PostgresLockMitigationOracle`` with Saleor's storefront catalog query as the app-level check."""

    def _product_catalog_available(self) -> bool:
        result = checkout_probe(self.problem)
        if not result.get("ok"):
            print(f"❌ Storefront checkout fails at {result.get('step')}: {str(result.get('errors'))[:300]}")
        return bool(result.get("ok"))


class PostgresLockContentionSaleor(PostgresLockContentionProductCatalog):
    """A session holds ``ACCESS EXCLUSIVE`` on ``product_product``; every catalog read queues behind it.

    As in the original, the holder is a one-shot Job in the hidden ``khaos``
    namespace (a "stuck catalog maintenance/migration" connecting as the
    application role) that takes the lock once and sleeps in its transaction;
    terminating the backend releases it for good. Saleor reads
    ``product_product`` on every storefront query and checkout step, so the
    whole storefront hangs while every pod stays Running.
    """

    TABLE = "product_product"
    HOLDER_JOB = "catalog-maintenance"
    HOLDER_SELECTOR = "app=catalog-maintenance"
    HOLDER_IMAGE = "postgres:16"

    def __init__(self, app_name: str = APP):
        self.problem_id = "postgres_lock_contention_saleor"
        self.faulty_service = [API]
        ported(
            self,
            app_name,
            component=f"{POSTGRES} (table public.{self.TABLE})",
            description=(
                f"One database session holds an ACCESS EXCLUSIVE lock on Saleor's `{self.TABLE}` table (PostgreSQL "
                "StatefulSet `postgres`, database `saleor`) inside a transaction it never commits (`LOCK TABLE ... "
                "IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(...)`, connected as the application role `saleor_app`). "
                "Every query that reads the product table queues behind the lock and never completes, so the "
                f"storefront catalog, product pages and every checkout step served by `{API}` hang until the "
                "client times out. The API and Postgres pods stay Running with normal CPU and memory; the cause "
                "is only visible inside the database (pg_locks / pg_stat_activity show the waiting sessions and the "
                "idle-in-transaction holder). The fix is to end the blocking session (pg_terminate_backend on its "
                "pid)."
            ),
            oracle_factory=SaleorCatalogLockMitigationOracle,
        )

    def _holder_manifest(self) -> dict:
        host = f"{POSTGRES}.{self.namespace}.svc.cluster.local"
        auth = f"PGPASSWORD={self.app.APP_PASSWORD} psql -h {host} -U {self.app.APP_ROLE} -d {self.app.DATABASE}"
        script = (
            f'until {auth} -c "SELECT 1" >/dev/null 2>&1; do sleep 2; done\n'
            f'{auth} -c "BEGIN; LOCK TABLE {self.TABLE} IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(86400);"\n'
        )
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self.HOLDER_JOB, "labels": {"app": self.HOLDER_JOB}},
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "metadata": {"labels": {"app": self.HOLDER_JOB}},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "maintenance",
                                "image": self.HOLDER_IMAGE,
                                "command": ["sh", "-c", script],
                            }
                        ],
                    },
                },
            },
        }

    def _admin_psql(self, sql: str) -> tuple[bool, str]:
        try:
            return True, self.app.psql(sql)
        except Exception as exc:
            # Keep only psql's stderr (the message also repeats the command).
            return False, str(exc).rsplit("': ", 1)[-1]

    def _catalog_read_status(self) -> str:
        ok, out = self._admin_psql(f"SET lock_timeout = 3000; SELECT count(*) FROM (SELECT 1 FROM {self.TABLE} LIMIT 1) t")
        if ok:
            return "ok"
        if "lock timeout" in out.lower() or "canceling statement due to lock" in out.lower():
            return "locked"
        print(f"catalog read failed: {out[-300:]}")
        return "other"

    def _terminate_lock_holder_backend(self) -> None:
        self._admin_psql(
            "SELECT pg_terminate_backend(l.pid) FROM pg_locks l JOIN pg_class c ON l.relation = c.oid "
            f"WHERE c.relname = '{self.TABLE}' AND l.mode = 'AccessExclusiveLock' AND l.pid <> pg_backend_pid()"
        )

    def _delete_holder(self) -> None:
        self.kubectl.exec_command(
            f"kubectl delete job {self.HOLDER_JOB} -n {self.HOLDER_NAMESPACE} --ignore-not-found --wait=false"
        )
        self.kubectl.exec_command(
            f"kubectl delete pod -n {self.HOLDER_NAMESPACE} -l {self.HOLDER_SELECTOR} --force --grace-period=0 "
            "--ignore-not-found"
        )

    @mark_fault_injected
    def inject_fault(self) -> bool:
        ensure_namespace(self, self.HOLDER_NAMESPACE)
        self._delete_holder()
        apply_manifest(self, self._holder_manifest(), self.HOLDER_NAMESPACE)
        if not self._wait_until(lambda: self._catalog_read_status() == "locked", timeout=300):
            raise RuntimeError("lock not confirmed within timeout; fault injection failed")
        print(f"{self.HOLDER_NAMESPACE}/{self.HOLDER_JOB} holds ACCESS EXCLUSIVE on {self.TABLE}")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        self._delete_holder()

        def released() -> bool:
            self._terminate_lock_holder_backend()
            return self._catalog_read_status() == "ok"

        if not self._wait_until(released, timeout=120):
            raise RuntimeError(f"{self.TABLE} still locked after terminating the holder")
        print(f"Terminated the lock holder on {self.TABLE}")
        return True


# ============================================================================= file_descriptor_exhaustion
class SaleorFDMitigationOracle(FDMitigationOracle):
    """``FDMitigationOracle`` plus the open-file limit the API processes actually run with.

    The log check alone samples one pod's last lines; the limit is the fault's
    state, read from ``/proc/<pid>/limits`` of every process in every API pod.
    """

    MIN_OPEN_FILES = 16384

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result.get("success"):
            return result
        problem = self.problem
        selector = problem.api_selector()
        pods = problem.app.pod_names(selector)
        if not pods:
            return self.fail("fault_still_present", detail="no running API pod")
        script = (
            "for p in /proc/[0-9]*; do [ -r $p/limits ] && "
            "awk '/Max open files/ {print $4}' $p/limits; done | sort -n | head -1"
        )
        for pod in pods:
            try:
                low = problem.app.exec_in(f"pod/{pod}", script).strip()
                limit = float("inf") if low in ("", "unlimited") else int(low)
            except Exception as exc:
                return self.fail_from_exception(exc, pod=pod)
            if limit < self.MIN_OPEN_FILES:
                print(f"❌ {pod} runs with an open-file limit of {limit} (< {self.MIN_OPEN_FILES})")
                return self.fail("fault_still_present", pod=pod, nofile=limit)
        print(f"✅ Every {problem.faulty_service} process may open >= {self.MIN_OPEN_FILES} files")
        return {"success": True}


class FileDescriptorExhaustionSaleor(FileDescriptorExhaustion):
    """``ulimit -n 1024`` on the Saleor API while a flood of idle TCP connections is held open.

    The original wrapped the Hotel Reservation frontend's entrypoint in
    ``ulimit -n 1024 && exec frontend`` and flooded it from the conductor with
    ~1,100 concurrent connections through a port-forward. Here the API's
    uvicorn command is wrapped the same way (each of its 4 worker processes
    inherits the 1024 soft limit), and the flood comes from a client pod in the
    hidden ``khaos`` namespace that keeps ~6,000 idle connections open to
    ``svc-saleor-api`` (uvicorn never times out a connection that has not sent
    a request). Workers hit EMFILE: accept() fails with ``Too many open files``
    and requests that need a new database/cache socket fail.
    """

    FLOODER = "api-connection-flood"
    CONNECTIONS = 6000
    FLOOD_SCRIPT = r"""
import os, socket, time
target = (os.environ["TARGET_HOST"], int(os.environ["TARGET_PORT"]))
want = int(os.environ["CONNECTIONS"])
held = []
while True:
    alive = []
    for s in held:
        try:
            s.setblocking(False)
            if s.recv(1) == b"":
                s.close()
                continue
        except BlockingIOError:
            pass
        except OSError:
            s.close()
            continue
        alive.append(s)
    held = alive
    opened = 0
    while len(held) < want and opened < 500:
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect(target)
            held.append(s)
        except OSError:
            s.close()
        opened += 1
    print(f"holding {len(held)} connections", flush=True)
    time.sleep(5)
"""

    def __init__(self, app_name: str = APP, faulty_service: str = API):
        self.faulty_service = faulty_service
        self.forced_ulimit = 1024
        self.flooder_thread = None
        ported(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{faulty_service}` deployment (Saleor's GraphQL API behind `{API_SERVICE}`) is exhausting its "
                "file descriptors. Its container command was wrapped as `/bin/sh -c 'ulimit -n "
                f"{self.forced_ulimit} && exec uvicorn saleor.asgi:application ...'`, so each uvicorn worker may "
                f"hold at most {self.forced_ulimit} open files, while a client floods the service with thousands of "
                "simultaneous, idle TCP connections. The workers run out of descriptors: accept() fails with "
                "`OSError: [Errno 24] Too many open files` and requests that need new database or cache sockets "
                "fail, so storefront and checkout requests time out or error. The root cause is the insufficient "
                f"open-file limit of {self.forced_ulimit} (restore the plain uvicorn command / raise the limit)."
            ),
            oracle_factory=SaleorFDMitigationOracle,
        )

    def api_selector(self) -> str:
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        return ",".join(f"{k}={v}" for k, v in sorted(deployment.spec.selector.match_labels.items()))

    def _deployment_json(self) -> dict:
        return json.loads(
            self.kubectl.exec_command_checked(f"kubectl get deployment {self.faulty_service} -n {self.namespace} -o json")
        )

    def _patch_container(self, command, args) -> None:
        patch = [
            {"op": "add", "path": "/spec/template/spec/containers/0/command", "value": command},
        ]
        patch.append(
            {"op": "add", "path": "/spec/template/spec/containers/0/args", "value": args}
            if args is not None
            else {"op": "remove", "path": "/spec/template/spec/containers/0/args"}
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json "
            f"-p {shlex.quote(json.dumps(patch))}"
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=600s", timeout=630
        )

    def _flooder_manifest(self) -> dict:
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.FLOODER, "labels": {"app": self.FLOODER}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": self.FLOODER}},
                "template": {
                    "metadata": {"labels": {"app": self.FLOODER}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "flood",
                                "image": "python:3.12-alpine",
                                "command": ["python3", "-u", "-c", self.FLOOD_SCRIPT],
                                "env": [
                                    {"name": "TARGET_HOST", "value": f"{API_SERVICE}.{self.namespace}.svc.cluster.local"},
                                    {"name": "TARGET_PORT", "value": "8000"},
                                    {"name": "CONNECTIONS", "value": str(self.CONNECTIONS)},
                                ],
                            }
                        ],
                    },
                },
            },
        }

    @mark_fault_injected
    def inject_fault(self):
        container = self._deployment_json()["spec"]["template"]["spec"]["containers"][0]
        saved = {"command": container.get("command"), "args": container.get("args")}
        if not saved["command"]:
            raise RuntimeError(f"{self.faulty_service} has no explicit command to wrap")
        state_file(self, "fd-exhaustion").write_text(json.dumps(saved))
        entrypoint = " ".join(shlex.quote(part) for part in [*saved["command"], *(saved["args"] or [])])
        self._patch_container(["/bin/sh", "-c"], [f"ulimit -n {self.forced_ulimit} && exec {entrypoint}"])
        print(f"{self.faulty_service} now runs with ulimit -n {self.forced_ulimit}")
        ensure_namespace(self, HIDDEN_NAMESPACE)
        apply_manifest(self, self._flooder_manifest(), HIDDEN_NAMESPACE)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.FLOODER} -n {HIDDEN_NAMESPACE} --timeout=300s", timeout=330
        )
        print(f"{HIDDEN_NAMESPACE}/{self.FLOODER} holds up to {self.CONNECTIONS} connections to {API_SERVICE}")

    @mark_fault_injected
    def recover_fault(self):
        saved = json.loads(state_file(self, "fd-exhaustion").read_text())
        self.kubectl.exec_command(f"kubectl delete deployment {self.FLOODER} -n {HIDDEN_NAMESPACE} --ignore-not-found")
        self._patch_container(saved["command"], saved["args"])
        print(f"Restored the {self.faulty_service} command and stopped the connection flood")


# ============================================================================= latent_sector_error
class LatentSectorErrorSaleor(Problem):
    """Blocks past the first page of the ``saleor`` database's relation files can no longer be read.

    The original stopped a MongoDB deployment, truncated its WiredTiger files
    to the 4 KiB header on the node and started it again, so reads of the
    missing blocks fail. Here the PostgreSQL StatefulSet is scaled to 0, every
    relation file of the ``saleor`` database (``base/<oid>/``, catalogs
    included) is truncated to its first 8 KiB page on the node, and the
    database is started again. The server comes up, but any session on the
    database fails reading the missing blocks (``could not read block N in
    file "base/...": read only 0 of 8192 bytes``), so every API request fails.

    A ``pg_dump`` taken just before (on the database's own volume, under
    ``backups/``) is the realistic way back; the harness also snapshots the
    data directory on the node so recovery restores it exactly.
    """

    STS = POSTGRES
    SNAPSHOT_ROOT = "/var/lib/sregym-snapshots"
    BACKUP_DIR = "/var/lib/postgresql/data/backups"

    def __init__(self, app_name: str = APP):
        self.faulty_service = self.STS
        ported(
            self,
            app_name,
            component=f"statefulset/{self.STS}",
            description=(
                f"The storage backing Saleor's PostgreSQL (`{self.STS}` StatefulSet, PVC `data-{self.STS}-0`) "
                "developed latent sector errors: everything past the first 8 KiB page of each relation file of "
                "the `saleor` database (`pgdata/base/<database oid>/`, system catalogs included) can no longer be "
                "read. PostgreSQL starts, but every session on the database fails when it reads those blocks "
                "(`could not read block N in file \"base/...\": read only 0 of 8192 bytes`), so the Saleor API and "
                "worker cannot load anything and every storefront and checkout request fails. The data must be "
                f"restored, e.g. by recreating the database from the `pg_dump` archive in `{self.BACKUP_DIR}` on "
                "the database volume."
            ),
            oracle_factory=SaleorFaultStateOracle,
        )

    def _volume_location(self) -> tuple[str, str]:
        core = self.kubectl.core_v1_api
        pvc = core.read_namespaced_persistent_volume_claim(f"data-{self.STS}-0", self.namespace)
        pv = core.read_persistent_volume(pvc.spec.volume_name)
        path = pv.spec.local.path if pv.spec.local else pv.spec.host_path.path
        node = None
        terms = pv.spec.node_affinity.required.node_selector_terms if pv.spec.node_affinity else []
        for term in terms or []:
            for expr in term.match_expressions or []:
                if expr.key == "kubernetes.io/hostname" and expr.values:
                    node = expr.values[0]
        if node is None:
            node = core.read_namespaced_pod(f"{self.STS}-0", self.namespace).spec.node_name
        return node, path

    def _scale(self, replicas: int) -> None:
        self.kubectl.exec_command_checked(
            f"kubectl scale statefulset/{self.STS} -n {self.namespace} --replicas={replicas}"
        )
        if replicas == 0:
            self.kubectl.exec_command(f"kubectl wait pod/{self.STS}-0 -n {self.namespace} --for=delete --timeout=180s")
        else:
            time.sleep(5)
            self.kubectl.exec_command(
                f"kubectl wait pod/{self.STS}-0 -n {self.namespace} --for=condition=Ready --timeout=300s"
            )

    def _node_script(self, node: str, script: str) -> str:
        ensure_namespace(self, HIDDEN_NAMESPACE)
        return self.kubectl.run_node_script_pod(
            node_name=node, namespace=HIDDEN_NAMESPACE, script=script, name_prefix="sregym-storage-fault", timeout=600
        )

    def _snapshot(self) -> str:
        return f"{self.SNAPSHOT_ROOT}/{self.namespace}-{self.STS}"

    def _orders(self) -> int:
        return int(self.app.psql("SELECT count(*) FROM order_order").strip())

    @mark_fault_injected
    def inject_fault(self):
        oid = self.app.psql(f"SELECT oid FROM pg_database WHERE datname = '{self.app.DATABASE}'").strip()
        orders = self._orders()
        stamp = time.strftime("%Y%m%d-%H%M")
        self.app.exec_in(
            f"pod/{self.STS}-0",
            f"mkdir -p {self.BACKUP_DIR} && PGPASSWORD={self.app.ADMIN_PASSWORD} pg_dump -h 127.0.0.1 "
            f"-U {self.app.ADMIN_ROLE} -Fc -f {self.BACKUP_DIR}/{self.app.DATABASE}-{stamp}.dump {self.app.DATABASE}",
            container="postgres",
            timeout=600,
        )
        node, path = self._volume_location()
        state_file(self, "latent-sector").write_text(json.dumps({"node": node, "path": path, "orders": orders}))
        self._scale(0)
        script = f"""set -e
cd "/host{path}"
rm -rf "/host{self._snapshot()}"; mkdir -p "/host{self._snapshot()}"
cp -a . "/host{self._snapshot()}/"
cd pgdata/base/{oid}
rm -f pg_internal.init
n=0
for f in *; do
    case "$f" in PG_VERSION|pg_filenode.map) continue;; esac
    [ -f "$f" ] || continue
    size=$(stat -c %s "$f")
    [ "$size" -gt 8192 ] || continue
    truncate -s 8192 "$f"; n=$((n+1))
done
echo "truncated $n relation files"
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        deadline = time.monotonic() + 120
        while True:
            try:
                self.app.psql("SELECT count(*) FROM order_order JOIN account_address a ON true", timeout=30)
            except Exception as exc:
                print(f"saleor database unreadable: {str(exc)[-200:]}")
                break
            if time.monotonic() > deadline:
                raise RuntimeError("the saleor database is still readable after truncation")
            time.sleep(5)
        print(f"Truncated the relation files of database {self.app.DATABASE} (oid {oid}) on {node}:{path}")

    @mark_fault_injected
    def recover_fault(self):
        saved = json.loads(state_file(self, "latent-sector").read_text())
        node, path = saved["node"], saved["path"]
        self._scale(0)
        script = f"""set -e
test -d "/host{self._snapshot()}/pgdata"
cd "/host{path}"
find . -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
cp -a "/host{self._snapshot()}/." .
rm -rf "/host{self._snapshot()}"
echo restored
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.STS} -n {self.namespace} --timeout=600s", timeout=630
        )
        # The API and worker hold broken pooled connections from the outage; recycle them.
        for deployment in (API, WORKER):
            self.app.rollout_restart("deployment", deployment)

    def fault_check(self, oracle) -> dict | None:
        sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.STS, self.namespace)
        if (sts.status.ready_replicas or 0) < 1:
            print(f"❌ {self.STS} is not ready")
            return oracle.fail("fault_still_present", statefulset=self.STS)
        try:
            listing = self.app.psql(
                "SELECT quote_ident(tablename) FROM pg_tables WHERE schemaname = 'public' ORDER BY 1", timeout=60
            )
        except Exception as exc:
            print(f"❌ Cannot list the tables of {self.app.DATABASE}: {str(exc)[-200:]}")
            return oracle.fail("fault_still_present", error=str(exc)[-300:])
        tables = [t.strip() for t in listing.splitlines() if t.strip()]
        unreadable = []
        for start in range(0, len(tables), 40):
            batch = tables[start : start + 40]
            try:
                self.app.psql("; ".join(f"SELECT count(*) FROM {t}" for t in batch), timeout=120)
            except Exception:
                for table in batch:
                    try:
                        self.app.psql(f"SELECT count(*) FROM {table}", timeout=60)
                    except Exception:
                        unreadable.append(table)
        if unreadable or not tables:
            print(f"❌ Unreadable tables in {self.app.DATABASE}: {unreadable[:10]} ({len(unreadable)} total)")
            return oracle.fail("fault_still_present", unreadable=unreadable[:20], tables=len(tables))
        path = state_file(self, "latent-sector")
        before = json.loads(path.read_text()).get("orders", 0) if path.exists() else 0
        if self._orders() < before:
            print(f"❌ Orders were lost: {before} before the fault, {self._orders()} now")
            return oracle.fail("fault_still_present", detail="orders lost", before=before, after=self._orders())
        print(f"✅ All {len(tables)} tables of {self.app.DATABASE} are readable")
        return require_checkout(self, oracle)


# ============================================================================= revoke_auth_mongodb-2 / storage_user_unregistered-2
# The worker connects to PostgreSQL as its own role (chart value
# saleor.worker.useDedicatedDbRole), and the load generator runs the async
# checkout lane: each order's ORDER_CREATED webhook, delivered by the Celery
# worker, must arrive back at the load generator.
ASYNC_LANE_VALUES = {"saleor": {"worker": {"useDedicatedDbRole": True}}}
ASYNC_LANE_PROFILE = ("lite_saleor_async", {"base": "saleor_async_eval", "soak_cycles": 4})
WORKER_ROLE = "saleor_worker_db"
WORKER_PASSWORD = "agentrepair-worker"
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")


class _WorkerRoleFault(Problem):
    """Base for faults on the Celery worker's dedicated PostgreSQL role (was a MongoDB user)."""

    COMPONENT = f"statefulset/{POSTGRES} (role `{WORKER_ROLE}`)"
    DESCRIPTION = ""

    def __init__(self, app_name: str = APP):
        self.faulty_service = WORKER
        ported(self, app_name, component=self.COMPONENT, description=self.DESCRIPTION, oracle_factory=SaleorFaultStateOracle)
        self.app.configure(ASYNC_LANE_VALUES)
        self.app.set_load_profile(*ASYNC_LANE_PROFILE)

    def _restart_worker(self) -> None:
        # Faithful to the originals, which restarted the client into the fault.
        self.app.psql(
            f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = '{WORKER_ROLE}'"
        )
        self.kubectl.exec_command(f"kubectl rollout restart deployment/{WORKER} -n {self.namespace}")

    def _missing_privileges(self) -> list[str]:
        rows = self.app.psql(
            "SELECT c.relname || ':' || p FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"CROSS JOIN unnest(ARRAY{list(DML)}) p WHERE n.nspname = 'public' AND c.relkind = 'r' "
            f"AND NOT has_table_privilege('{WORKER_ROLE}', c.oid, p) ORDER BY 1"
        )
        return [r.strip() for r in rows.splitlines() if r.strip()]

    def _role_exists(self) -> bool:
        return self.app.psql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{WORKER_ROLE}'").strip() == "1"

    def _delivery_round_trip(self, oracle) -> dict | None:
        """A probe checkout's webhook deliveries must be completed by the worker (broker + worker + DB role).

        The API records each async delivery as a pending ``core_eventdelivery``
        row and queues a Celery task; the worker sends it and deletes the row
        on success. So: every delivery created since the probe started must
        leave ``pending``/``failed``, and the worker must log a sent payload.
        """
        since = self.app.psql("SELECT now()").strip()
        started = time.time()
        probe = checkout_probe(self)
        if not probe.get("ok"):
            print(f"❌ Guest checkout fails at {probe.get('step')}: {str(probe.get('errors'))[:300]}")
            return oracle.fail("checkout_probe_failed", step=probe.get("step"))
        deadline = time.monotonic() + 90
        while True:
            left = self.app.psql(
                f"SELECT count(*) FROM core_eventdelivery WHERE created_at >= '{since}' AND status <> 'success'"
            ).strip()
            window = int(time.time() - started) + 5
            sent = self.kubectl.exec_command(
                f"kubectl logs deployment/{WORKER} -n {self.namespace} --since={window}s"
            ).count("Payload sent to")
            if int(left or 0) == 0 and sent > 0:
                print(f"✅ The worker delivered the new webhook events ({sent} payloads sent since the probe)")
                return None
            if time.monotonic() > deadline:
                print(f"❌ Async round trip incomplete after 90s: {left} deliveries undelivered, {sent} payloads sent")
                return oracle.fail(
                    "fault_still_present", detail="async round trip failed", undelivered=left, sent=sent
                )
            time.sleep(5)

    def fault_check(self, oracle) -> dict | None:
        if not self._role_exists():
            print(f"❌ Role {WORKER_ROLE} does not exist")
            return oracle.fail("fault_still_present", role=WORKER_ROLE, detail="role missing")
        try:
            self.app.psql("SELECT 1", user=WORKER_ROLE, password=WORKER_PASSWORD)
        except Exception as exc:
            print(f"❌ {WORKER_ROLE} cannot log in with the worker's password: {str(exc)[-200:]}")
            return oracle.fail("fault_still_present", role=WORKER_ROLE, detail="login failed")
        missing = self._missing_privileges()
        if missing:
            print(f"❌ {WORKER_ROLE} lacks {len(missing)} table privileges, e.g. {missing[:5]}")
            return oracle.fail("fault_still_present", role=WORKER_ROLE, missing=missing[:20])
        return self._delivery_round_trip(oracle)


class RevokeAuthWorkerRoleSaleor(_WorkerRoleFault):
    """``REVOKE SELECT, INSERT, UPDATE, DELETE`` on every table from the worker's DB role."""

    COMPONENT = f"statefulset/{POSTGRES} (grants of role `{WORKER_ROLE}`)"
    DESCRIPTION = (
        f"Database access for Saleor's Celery worker was explicitly revoked in PostgreSQL (`{POSTGRES}`, database "
        f"`saleor`): `REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {WORKER_ROLE}`. "
        f"`{WORKER}` connects as this dedicated role (its own DATABASE_URL), which still exists and "
        "authenticates, so the worker starts and consumes tasks from RabbitMQ, but every database-backed task "
        "fails with `permission denied for table ...`. Asynchronous work such as webhook deliveries "
        "(ORDER_CREATED for every new order) stops, while the API (role `saleor_app`) keeps taking orders. "
        f"Fix: grant the DML privileges back to `{WORKER_ROLE}`."
    )

    @mark_fault_injected
    def inject_fault(self):
        self.app.psql(f"REVOKE {', '.join(DML)} ON ALL TABLES IN SCHEMA public FROM {WORKER_ROLE}")
        if not self._missing_privileges():
            raise RuntimeError(f"{WORKER_ROLE} still holds its privileges")
        print(f"Revoked {DML} on all public tables from {WORKER_ROLE}")
        self._restart_worker()

    @mark_fault_injected
    def recover_fault(self):
        self.app.psql(f"GRANT {', '.join(DML)} ON ALL TABLES IN SCHEMA public TO {WORKER_ROLE}")
        print(f"Granted {DML} on all public tables back to {WORKER_ROLE}")


class StorageUserUnregisteredWorkerRoleSaleor(_WorkerRoleFault):
    """``DROP ROLE`` of the worker's DB role; the worker is restarted into the failure."""

    COMPONENT = f"statefulset/{POSTGRES} (role `{WORKER_ROLE}`)"
    DESCRIPTION = (
        f"The PostgreSQL role Saleor's Celery worker (`{WORKER}`) connects as, `{WORKER_ROLE}` (password from "
        "the worker's DATABASE_URL), is missing: it was dropped from the database server (`postgres` "
        "StatefulSet) together with its grants. The worker cannot authenticate (`password authentication failed "
        f"for user \"{WORKER_ROLE}\"` / role does not exist), so every task that needs the database fails and "
        "asynchronous work such as webhook deliveries (ORDER_CREATED for every new order) stops, while the API "
        "(role `saleor_app`) keeps serving. Fix: recreate the role with the worker's password and grant it "
        "CONNECT, schema USAGE and SELECT/INSERT/UPDATE/DELETE on the tables (plus sequence usage)."
    )

    @mark_fault_injected
    def inject_fault(self):
        self.app.psql(f"DROP OWNED BY {WORKER_ROLE}")
        self.app.psql(f"DROP ROLE {WORKER_ROLE}")
        print(f"Dropped role {WORKER_ROLE}")
        self._restart_worker()

    @mark_fault_injected
    def recover_fault(self):
        if not self._role_exists():
            self.app.psql(
                f"CREATE ROLE {WORKER_ROLE} LOGIN PASSWORD '{WORKER_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
        for sql in (
            f"GRANT CONNECT ON DATABASE {self.app.DATABASE} TO {WORKER_ROLE}",
            f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}",
            f"GRANT {', '.join(DML)} ON ALL TABLES IN SCHEMA public TO {WORKER_ROLE}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {WORKER_ROLE}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE saleor_app IN SCHEMA public GRANT {', '.join(DML)} ON TABLES TO {WORKER_ROLE}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE saleor_app IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {WORKER_ROLE}",
        ):
            self.app.psql(sql)
        print(f"Recreated {WORKER_ROLE} with its grants")


PORTS: dict[str, tuple[str, type]] = {
    "astronomy_shop_payment_service_failure": (
        "astronomy_shop_payment_service_failure_saleor",
        PaymentGatewayDisabledSaleor,
    ),
    "astronomy_shop_cart_service_failure": ("astronomy_shop_cart_service_failure_saleor", ChannelDeactivatedSaleor),
    "astronomy_shop_product_catalog_service_failure": (
        "astronomy_shop_product_catalog_service_failure_saleor",
        CatalogUnpublishedSaleor,
    ),
    "trainticket_f22_sql_column_name_mismatch_error": (
        "trainticket_f22_sql_column_name_mismatch_error_saleor",
        SchemaColumnDriftSaleor,
    ),
    "integer_overflow_primary_key_astronomy_shop": ("integer_overflow_primary_key_saleor", IntegerOverflowPrimaryKeySaleor),
    "postgres_lock_contention_product_catalog": ("postgres_lock_contention_saleor", PostgresLockContentionSaleor),
    "file_descriptor_exhaustion": ("file_descriptor_exhaustion_saleor", FileDescriptorExhaustionSaleor),
    "latent_sector_error": ("latent_sector_error_saleor", LatentSectorErrorSaleor),
    "revoke_auth_mongodb-2": ("revoke_auth_saleor", RevokeAuthWorkerRoleSaleor),
    "storage_user_unregistered-2": ("storage_user_unregistered_saleor", StorageUserUnregisteredWorkerRoleSaleor),
}
