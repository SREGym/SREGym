"""The Larkbank empty-nodes problem: its customers, its manifests, what the agent is told, and its grader."""

import copy
import json

import pytest

from sregym.conductor.oracles.apps.larkbank.ring_recovery import (
    LATE_SECONDS,
    REASONS,
    TWICE_GRACE_SECONDS,
    RingRecoveryOracle,
    judge,
    longest_gap,
    shown,
)
from sregym.conductor.problems.apps.larkbank.empty_nodes import LarkbankEmptyNodesProblem
from sregym.service.apps.larkbank import application
from sregym.service.apps.larkbank.application import (
    EVERY_SECONDS,
    HIDDEN_NAMESPACE,
    NODES,
    NODES_WANTED,
    OVERRIDES,
    RING_PLAN,
    TIERS,
    LarkbankApplication,
    effective_auto_bootstrap,
    parse_auto_bootstrap,
    tokens,
    world_spec,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(application, "KubeCtl", lambda: None)
    monkeypatch.setenv("SREGYM_LARKBANK_IMAGE", "example.test/larkbank:dev")
    monkeypatch.setenv("SREGYM_LARKBANK_HIDDEN_IMAGE", "example.test/larkbank-hidden:dev")
    return LarkbankApplication("small")


# ---- the customers ------------------------------------------------------------


def test_the_customers_are_the_same_every_time_and_do_every_kind_of_thing():
    tier = TIERS["small"]
    spec = world_spec(tier)
    assert spec == world_spec(tier)
    assert len({c["id"] for c in spec}) == len(spec) == tier["customers"]
    accounts = [(p["sort_code"], p["account_number"]) for c in spec for p in c["payees"]]
    accounts += [(c["add_payee"]["sort_code"], c["add_payee"]["account_number"]) for c in spec if c["add_payee"]]
    assert len(set(accounts)) == len(accounts)  # what a payee says is its own: the same account twice is a copy
    for c in spec:
        assert c["payees"] and c["orders"]
        assert len({(o["payee"], o["amount"], o["reference"]) for o in c["orders"]}) == len(c["orders"])
        assert all(0 <= o["payee"] < len(c["payees"]) for o in c["orders"])
        if c["cancel_at"] >= 0:
            # Nobody cancels what they have set up twice.
            assert not c["redo"] and 0 <= c["cancel"] < len(c["orders"])
    assert sum(c["add_at"] >= 0 for c in spec) == tier["adders_early"] + tier["adders"]
    # Enough customers add something while the new nodes are the only replicas of a fifth of the ring that
    # some of it is always there to lose (the chance that none of them is in that fifth is under 1 in 1,000).
    early = sum(0 <= c["add_at"] <= tier["wild_seconds"] for c in spec)
    assert early == tier["adders_early"] and 0.8**early < 0.001
    assert sum(c["cancel_at"] >= 0 for c in spec) == tier["cancellers"]
    hasty = sum(c["redo"] == "hasty" for c in spec) / len(spec)
    patient = sum(c["redo"] == "patient" for c in spec) / len(spec)
    assert abs(hasty - tier["hasty"]) < 0.08 and abs(patient - tier["patient"]) < 0.08
    # Customers go on adding and cancelling for as long as the agent may take.
    assert max(c["add_at"] for c in spec) > 1200 and max(c["cancel_at"] for c in spec) > 1200


def test_the_ring_plan_puts_a_fifth_of_the_ring_wholly_on_new_nodes():
    assert len(RING_PLAN) == NODES_WANTED and list(RING_PLAN) == sorted(RING_PLAN)
    planned = tokens()
    assert planned == sorted(planned) and all(-(2**63) <= t < 2**63 for t in planned)
    # With three copies, a range's replicas are the node that ends it and the next two round the ring.
    new, share = set(range(NODES, NODES_WANTED)), {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0}
    for node, end in enumerate(RING_PLAN):
        width = (end - RING_PLAN[node - 1]) % 1
        replicas = {(node + k) % NODES_WANTED for k in range(3)}
        share[len(replicas & new)] += width
    assert abs(sum(share.values()) - 1) < 1e-9
    assert 0.15 <= share[3] <= 0.25  # nothing there can be read, and nothing heals it
    assert 0.2 <= share[2] <= 0.35  # read now and then, until a read repairs it


def test_the_seed_writes_what_customers_set_up_long_ago(app):
    seed = app.seed_cql(now=1_800_000_000)
    assert seed.count("INSERT INTO bank.customer") == len(app.spec)
    assert seed.count("INSERT INTO bank.payee") == sum(len(c["payees"]) for c in app.spec)
    assert seed.count("INSERT INTO bank.standing_order") == sum(len(c["orders"]) for c in app.spec)
    assert seed.count("USING TIMESTAMP") == seed.count("INSERT INTO")
    # Nothing a customer will only do later is in it.
    for c in app.spec:
        if c["add_payee"]:
            assert c["add_payee"]["account_number"] not in seed


# ---- the manifests -------------------------------------------------------------


def test_new_nodes_start_together_and_without_taking_their_data(app):
    config = app.ring_config_documents()[0]["data"]
    assert parse_auto_bootstrap(config["overrides.yaml"]) is False
    assert config["tokens"].split() == [str(t) for t in tokens()]
    assert "overrides.yaml" in config["start.sh"] and "initial_token" in config["start.sh"]
    ring = app.ring_documents()[1]
    assert ring["spec"]["replicas"] == NODES and ring["spec"]["podManagementPolicy"] == "Parallel"
    assert ring["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "data"
    container = ring["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["sh", "/etc/ring/start.sh"]
    assert {"name": "CASSANDRA_NUM_TOKENS", "value": "1"} in container["env"]
    fixed = app.ring_config_documents(OVERRIDES.replace("auto_bootstrap: false", "auto_bootstrap: true"))[0]["data"]
    assert parse_auto_bootstrap(fixed["overrides.yaml"]) is True


def test_what_a_cassandra_yaml_sets_auto_bootstrap_to():
    assert parse_auto_bootstrap("num_tokens: 1\n") is True
    assert parse_auto_bootstrap("auto_bootstrap: false\n") is False
    assert parse_auto_bootstrap("# auto_bootstrap: false\n") is True
    assert parse_auto_bootstrap("auto_bootstrap: false\nauto_bootstrap: true  # fixed\n") is True
    assert parse_auto_bootstrap("auto_bootstrap: true\nauto_bootstrap: False\n") is False


def test_jvm_flags_override_the_file_as_cassandra_reads_them():
    on, off = "auto_bootstrap: true\n", "auto_bootstrap: false\n"
    assert effective_auto_bootstrap(["java", "-Xmx320M"], off) is False
    assert effective_auto_bootstrap(["-Dcassandra.auto_bootstrap=true"], off) is True
    assert effective_auto_bootstrap(["-Dcassandra.auto_bootstrap=false"], on) is False
    assert effective_auto_bootstrap(["-Dcassandra.settings.auto_bootstrap=true"], off) is True
    assert (
        effective_auto_bootstrap(["-Dcassandra.settings.auto_bootstrap=true", "-Dcassandra.auto_bootstrap=false"], on)
        is False
    )


def test_the_test_node_is_the_next_node_as_the_template_now_starts_it_cut_off_from_the_ring(app, monkeypatch):
    ring = app.ring_documents()[1]
    template = ring["spec"]["template"]["spec"]
    template["volumes"] = [{"name": "config", "configMap": {"name": "ring-config-v2"}}]
    template["containers"][0]["env"].append({"name": "JVM_EXTRA_OPTS", "value": "-Dcassandra.auto_bootstrap=true"})
    monkeypatch.setattr(app, "command", lambda *args, **kwargs: json.dumps(ring))
    policy, pod = app.probe_documents()
    assert policy["kind"] == "NetworkPolicy" and set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in policy["spec"] and "egress" not in policy["spec"]
    assert policy["spec"]["podSelector"]["matchLabels"] == pod["metadata"]["labels"]
    spec = pod["spec"]
    assert spec["hostname"] == f"ring-{NODES}" and spec["restartPolicy"] == "Never"
    assert {"name": "config", "configMap": {"name": "ring-config-v2"}} in spec["volumes"]
    assert {"name": "data", "emptyDir": {}} in spec["volumes"]
    container = spec["containers"][0]
    assert container["command"] == ["sh", "/etc/ring/start.sh"] and "readinessProbe" not in container
    env = {var["name"]: var.get("value") for var in container["env"]}
    assert env["CASSANDRA_SEEDS"] == "127.0.0.1" and env["CASSANDRA_CLUSTER_NAME"] == "larkbank-probe"
    assert env["JVM_EXTRA_OPTS"] == "-Dcassandra.auto_bootstrap=true"
    assert "app" not in pod["metadata"]["labels"]


def test_manifests_keep_the_hidden_side_out_of_the_namespace(app):
    visible = (
        app.ring_config_documents()
        + app.ring_documents()
        + app.api_documents()
        + app.scheduler_documents()
        + app.rail_link_documents("10.0.0.9")
    )
    text = json.dumps(visible)
    assert "larkbank-hidden" not in text and HIDDEN_NAMESPACE not in text and "world" not in text
    assert all(d["metadata"].get("namespace") is None for d in visible)
    hidden = app.world_documents("rk_live_x")
    assert all(d["metadata"]["namespace"] == HIDDEN_NAMESPACE for d in hidden)
    assert hidden[1]["spec"]["template"]["spec"]["containers"][0]["image"] == "example.test/larkbank-hidden:dev"
    spec = json.loads(hidden[0]["data"]["world.json"])
    assert {c["id"] for c in spec} == {c["id"] for c in app.spec}
    # What customers will do is known to the world only.
    assert "redo" in spec[0] and "redo" not in app.seed_cql()


# ---- what the agent is told ---------------------------------------------------------


def test_the_note_states_what_is_owed_and_not_what_customers_have_done(app):
    told = app.handover_description("13:10")
    assert told.startswith(app.get_app_json()["Desc"])
    for owed in (
        "exactly as they left it",
        "whatever they have done in the app since stays done",
        "paid when it is due, once",
        "The app has to stay up",
        "in the ring and carrying their share",
        "adding a node must never do this again",
    ):
        assert owed in told
    # Nothing about customers setting things up again, nor the cause, nor how to take a node out.
    hints = (
        "again themselves",
        "set up again",
        "set them up",
        "re-create",
        "recreate",
        "duplicate",
        "twice",
        "auto_bootstrap",
        "bootstrap",
        "decommission",
        "removenode",
        "token",
        "quorum",
    )
    note = told[len(app.get_app_json()["Desc"]) :].lower()
    assert not any(hint in note for hint in hints)


def test_the_problem_is_registered_and_names_the_cause(app, monkeypatch):
    from sregym.conductor.problems import registry

    monkeypatch.setattr(registry, "KubeCtl", lambda: None, raising=False)
    assert "larkbank_empty_nodes" in registry.ProblemRegistry().PROBLEM_REGISTRY
    problem = LarkbankEmptyNodesProblem("small")
    assert isinstance(problem.mitigation_oracle, RingRecoveryOracle)
    assert "auto_bootstrap: false" in str(problem.root_cause) and "QUORUM" in str(problem.root_cause)
    problem.recover_fault()  # before any incident: nothing to do, and nothing raised


# ---- the grader ------------------------------------------------------------------------

SUBMITTED = 5000.0
ENDED = SUBMITTED + 60
RING = [{"state": "UN", "address": f"10.0.0.{n}", "host_id": str(n)} for n in range(NODES_WANTED)]
SETTING = {"planned": True}


def payee(n, account, copy_of=""):
    return {"id": f"pe_{n}{copy_of}", "name": "Landlord", "sort_code": "20-45-77", "account_number": account}


def order(n, payee_id, amount=50000, reference="RENT 1"):
    return {"id": f"so_{n}", "payee_id": payee_id, "amount": amount, "reference": reference}


def item(kind, key, **more):
    return {"kind": kind, "key": key, "origin": "before", **more}


def world():
    """Four customers after a right repair.

    A  saw everything throughout
    B  could see nothing, set a payee and its standing order up again; one copy of each is left
    C  added a payee and a standing order since, and cancelled an old standing order
    D  could see nothing and waited
    """

    def paid(customer, key, order_id, at):
        sort_code, account, amount, reference = key.split("|")
        return {"customer": customer, "sort_code": sort_code, "account_number": account, "amount": float(amount),
                "reference": reference, "standing_order_id": order_id, "at": at}  # fmt: skip

    a, b, c, c_new, d = (
        "20-45-77|10000001",
        "20-45-77|10000002",
        "20-45-77|10000003",
        "20-45-77|10000033",
        "20-45-77|10000004",
    )
    rent = "|50000|RENT 1"
    truth = {
        "customers": [
            {"id": "A", "redo": "", "items": [item("payee", a), item("standing_order", a + rent)]},
            {
                "id": "B",
                "redo": "hasty",
                "items": [
                    item("payee", b, redone=4000.0, made_ids=["pe_2x"], twice_at=4700.0),
                    item("standing_order", b + rent, redone=4000.0, made_ids=["so_2x"], twice_at=4700.0),
                ],
            },
            {
                "id": "C",
                "redo": "",
                "items": [
                    item("payee", c),
                    item("standing_order", c + rent, cancelled=4500.0),
                    {"kind": "payee", "key": c_new, "origin": "new", "made_at": 4400.0},
                    {"kind": "standing_order", "key": c_new + "|9000|NEW 7", "origin": "new", "made_at": 4400.0},
                ],
            },
            {"id": "D", "redo": "", "items": [item("payee", d), item("standing_order", d + rent)]},
        ],
        "payments": [
            paid("A", a + rent, "so_1", ENDED - 100),
            paid("B", b + rent, "so_2x", 4001),
            paid("B", b + rent, "so_2", 4710),  # the old copy, as soon as it could be read: not held against anybody
            paid("B", b + rent, "so_2", ENDED - 50),
            paid("C", c + rent, "so_3", 4300),
            paid("C", c_new + "|9000|NEW 7", "so_33", ENDED - 80),
            paid("D", d + rent, "so_4", ENDED - 20),
        ],
        "reads": [],
    }

    def view(customer, payees, orders):
        return {"customer": customer, "payees_status": 200, "standing_orders_status": 200, "payees": payees,
                "standing_orders": orders}  # fmt: skip

    final = [
        view("A", [payee(1, "10000001")], [order(1, "pe_1")]),
        view("B", [payee(2, "10000002")], [order(2, "pe_2")]),
        view("C", [payee(3, "10000003"), payee(33, "10000033")], [order(33, "pe_33", 9000, "NEW 7")]),
        view("D", [payee(4, "10000004")], [order(4, "pe_4")]),
    ]
    return truth, final


def reasons(truth, final, ring=None, setting=None):
    found = judge(
        truth, final, SUBMITTED, ENDED, RING if ring is None else ring, SETTING if setting is None else setting
    )
    return {reason: detail["count"] for reason, detail in found.items()}


def test_a_right_repair_has_nothing_wrong():
    assert reasons(*world()) == {}


def test_what_the_app_shows_is_counted_by_what_it_says():
    payees, orders = shown(
        {
            "payees": [payee(1, "10000001"), payee(1, "10000001", "x"), payee(2, "10000002")],
            "standing_orders": [order(1, "pe_1"), order(2, "pe_1x"), order(3, "pe_gone")],
        }
    )
    assert payees == {"20-45-77|10000001": 2, "20-45-77|10000002": 1}
    assert orders == {"20-45-77|10000001|50000|RENT 1": 2}  # the one whose payee is gone pays nobody


def test_data_a_customer_had_and_cannot_see_is_missing():
    truth, final = world()
    final[3]["payees"], final[3]["standing_orders"] = [], []
    assert reasons(truth, final) == {"customer_data_missing": 2}


def test_what_a_customer_added_since_and_cannot_see_is_a_change_lost():
    truth, final = world()
    final[2]["payees"].pop()
    final[2]["standing_orders"].pop()
    assert reasons(truth, final) == {"customer_changes_lost": 2}


def test_a_cancelled_standing_order_that_is_back():
    truth, final = world()
    final[2]["standing_orders"].append(order(3, "pe_3"))
    assert reasons(truth, final) == {"cancelled_came_back": 1}


def test_both_copies_left_is_set_up_twice_and_either_one_may_stand():
    truth, final = world()
    final[1]["payees"].append(payee(2, "10000002", "x"))
    final[1]["standing_orders"].append(order("2x", "pe_2x"))
    assert reasons(truth, final) == {"set_up_twice": 2}
    # The copy the customer made, and not the old one: as right as the other way round.
    final[1]["payees"], final[1]["standing_orders"] = [payee(2, "10000002", "x")], [order("2x", "pe_2x")]
    assert reasons(truth, final) == {}
    # The payee kept twice and the standing order once.
    final[1]["payees"].append(payee(2, "10000002"))
    assert reasons(truth, final) == {"set_up_twice": 1}
    # Both copies of the standing order removed: the customer no longer has it.
    final[1]["payees"], final[1]["standing_orders"] = [payee(2, "10000002")], []
    assert reasons(truth, final) == {"customer_data_missing": 1}


def test_a_standing_order_whose_payee_was_removed_is_not_there():
    truth, final = world()
    final[1]["payees"] = [payee(2, "10000002", "x")]  # the standing order kept still names the other copy
    assert reasons(truth, final) == {"customer_data_missing": 1}


def test_what_a_customer_set_up_again_just_before_the_submission_is_not_held_against_the_repair():
    truth, final = world()
    final[1]["payees"].append(payee(2, "10000002", "x"))
    for thing in truth["customers"][1]["items"]:
        thing["redone"] = SUBMITTED - LATE_SECONDS + 1
    assert reasons(truth, final) == {}
    for thing in truth["customers"][1]["items"]:
        thing["redone"] = SUBMITTED - LATE_SECONDS - 1
    assert reasons(truth, final) == {"set_up_twice": 1}


def test_paid_from_both_copies_once_the_customer_had_seen_both_for_a_while():
    truth, final = world()
    both = copy.deepcopy(truth["payments"][3])
    both.update(standing_order_id="so_2x", at=4700 + TWICE_GRACE_SECONDS + 5)
    truth["payments"].append(both)
    assert reasons(truth, final) == {"paid_twice": 1}
    # Both copies paying before then is the old data coming back into view, not the repair.
    truth["payments"][-1]["at"] = 4700 + TWICE_GRACE_SECONDS - 5
    truth["payments"][3]["at"] = ENDED - 50
    assert reasons(truth, final) == {}


def test_a_pair_already_seen_twice_at_the_handover_is_given_its_time_from_the_handover():
    truth, final = world()
    both = copy.deepcopy(truth["payments"][3])
    both.update(standing_order_id="so_2x", at=4700 + TWICE_GRACE_SECONDS + 5)
    truth["payments"].append(both)
    truth["payments"][3]["at"] = 4700 + TWICE_GRACE_SECONDS + 6
    found = judge(truth, final, SUBMITTED, ENDED, RING, SETTING, handover_at=4750.0)
    assert found == {}  # only five and six seconds past three minutes from the first sight, not from the handover
    found = judge(truth, final, SUBMITTED, ENDED, RING, SETTING, handover_at=4600.0)
    assert list(found) == ["paid_twice"]


def test_payments_sent_to_the_rail_by_hand_are_matched_by_what_they_pay():
    truth, final = world()
    # The rail's API asks only for the payee, amount and reference: no customer, no standing order.
    for payment in truth["payments"]:
        if payment["customer"] == "A":
            payment.pop("customer")
            payment.pop("standing_order_id")
    truth["payments"].append({"payment_id": "x", "sort_code": "99-99-99", "account_number": "1", "amount": 5,
                              "reference": "NOBODY", "at": ENDED - 10})  # fmt: skip
    assert reasons(truth, final) == {}  # A's hand-sent payment still counts as paid when due
    # A catch-up payment by hand is not a second copy of a standing order.
    by_hand = copy.deepcopy(truth["payments"][3])
    by_hand.pop("customer")
    by_hand.pop("standing_order_id")
    by_hand["at"] = 4700 + TWICE_GRACE_SECONDS + 5
    truth["payments"].append(by_hand)
    assert reasons(truth, final) == {}


def test_a_standing_order_not_paid_in_its_last_period():
    truth, final = world()
    truth["payments"][0]["at"] = ENDED - EVERY_SECONDS - 60
    assert reasons(truth, final) == {"standing_orders_not_paid": 1}
    # One the customer added moments ago has not come due for the scheduler yet.
    truth["payments"][0]["at"] = ENDED - 100
    truth["payments"] = [p for p in truth["payments"] if p["customer"] != "C" or p["reference"] != "NEW 7"]
    assert reasons(truth, final) == {"standing_orders_not_paid": 1}
    truth["customers"][2]["items"][3]["made_at"] = ENDED - 10
    assert reasons(truth, final) == {}


def test_an_app_that_does_not_answer_a_customer():
    truth, final = world()
    final[0]["payees_status"] = 503
    assert reasons(truth, final) == {"app_not_answering": 1}


def test_the_three_nodes_have_to_be_back_and_a_new_node_has_to_take_its_data():
    truth, final = world()
    assert reasons(truth, final, ring=RING[:NODES]) == {"nodes_missing": 1}
    leaving = [dict(node) for node in RING]
    leaving[5]["state"] = "UL"
    assert reasons(truth, final, ring=leaving) == {"nodes_missing": 1}
    assert reasons(truth, final, setting={"planned": False}) == {"adding_a_node_would_repeat": 1}
    assert reasons(truth, final, setting={"planned": None}) == {"adding_a_node_would_repeat": 1}


def test_reasons_come_in_their_order():
    truth, final = world()
    final[3]["payees"], final[3]["standing_orders"] = [], []
    final[1]["payees"].append(payee(2, "10000002", "x"))
    found = judge(truth, final, SUBMITTED, ENDED, RING[:NODES], SETTING)
    assert (
        list(found) == [r for r in REASONS if r in found] == ["customer_data_missing", "set_up_twice", "nodes_missing"]
    )
    assert found["customer_data_missing"]["sample"][0] == {"customer": "D", "payee": "20-45-77|10000004"}


def test_gaps_in_service():
    times = [10, 11, 12, 30, 31]
    assert longest_gap(times, 10, 31) == 18
    assert longest_gap(times, 0, 40) == 18
    assert longest_gap([], 0, 25) == 25
    assert longest_gap(times, 10, 31, pauses=[(13, 29)]) == 2
