"""Saleor: checkout."""

from __future__ import annotations

import json

from sregym.conductor.oracles.apps.saleor.state import SaleorFaultStateOracle
from sregym.conductor.problems.apps.saleor.helpers import (
    API,
    APP,
    CHANNEL,
    DUMMY_GATEWAY,
    POSTGRES,
    WORKER,
    admin_gql,
    expect_checkout_failure,
    mutation_ok,
    require_checkout,
    state_file,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port
from sregym.utils.decorators import mark_fault_injected


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
        configure_port(
            self,
            app_name,
            component=f"payment plugin `{DUMMY_GATEWAY}` configuration for channel `{CHANNEL}` (`plugins_pluginconfiguration`, statefulset/{POSTGRES})",
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


class ChannelDeactivatedSaleor(Problem):
    """Analogue of Astronomy Shop's ``cartFailure`` flag: the storefront channel is deactivated.

    Saleor scopes carts (checkouts), prices and product visibility to a sales
    channel. With ``default-channel`` inactive, ``checkoutCreate`` is rejected
    and anonymous catalog queries on the channel return no products, so no
    shopper can put anything in a cart.
    """

    def __init__(self, app_name: str = APP):
        self.faulty_service = API
        configure_port(
            self,
            app_name,
            component=f"sales channel `{CHANNEL}` (`channel_channel`, statefulset/{POSTGRES})",
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
            self,
            "mutation($id:ID!){channelDeactivate(id:$id){channel{isActive} errors{message}}}",
            {"id": channel["id"]},
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
        configure_port(
            self,
            app_name,
            component=(
                f"product channel listings of categories `{'`, `'.join(self.CATEGORIES)}` in channel `{CHANNEL}` "
                f"(`product_productchannellisting`, statefulset/{POSTGRES})"
            ),
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
        configure_port(
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

    def _address_snapshot(self) -> dict:
        rows = self.app.psql(
            f"SELECT COALESCE(jsonb_object_agg(a.id::text, to_jsonb(a)), '{{}}'::jsonb)::text FROM {self.TABLE} a"
        )
        return json.loads(rows)

    @mark_fault_injected
    def inject_fault(self):
        state_file(self, "schema-drift").write_text(json.dumps({"addresses": self._address_snapshot()}))
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
        if not path.exists():
            return oracle.fail("address_baseline_unavailable")
        before = json.loads(path.read_text())["addresses"]
        after = self._address_snapshot()
        changed = [
            row_id
            for row_id, original in before.items()
            if row_id not in after
            or any(field not in after[row_id] or after[row_id][field] != value for field, value in original.items())
        ]
        if changed:
            print(f"❌ {len(changed)} original addresses are missing or changed")
            return oracle.fail("original_addresses_changed", address_ids=changed)
        return require_checkout(self, oracle)
