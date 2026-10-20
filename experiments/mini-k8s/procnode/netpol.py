"""NetworkPolicy enforcement for procnode pods with plain iptables.

Every pod's traffic is routed through the host (see procnode's /32 veths), so
pod-to-pod packets traverse the host FORWARD chain after kube-proxy's DNAT and
host-to-pod packets traverse OUTPUT. Policies compile to one chain per isolated
pod and direction:

  PN-FWD  (jumped from FORWARD)   established -> ACCEPT
                                  -s <pod> -> PN-E-<id>   egress: RETURN if allowed else DROP
                                  -d <pod> -> PN-I-<id>   ingress: ACCEPT if allowed else DROP
  PN-OUT  (jumped from OUTPUT)    probe mark -> RETURN (kubelet probes bypass policy)
                                  established -> ACCEPT
                                  -d <pod> -> PN-I-<id>

Host-originated traffic is policed like traffic from another node, which is
what a webhook or metrics scrape from a host-network apiserver looks like on a
multi-node cluster.

Supported: pod/namespace selectors (matchLabels + matchExpressions), ipBlock
with except, numeric and named (ingress) ports, endPort, TCP/UDP/SCTP.
"""

import hashlib
import ipaddress
import subprocess

PROBE_MARK = 0x5050


def matches(selector, labels):
    """Kubernetes LabelSelector semantics; None matches nothing, {} everything."""
    if selector is None:
        return False
    labels = labels or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get("matchExpressions") or []:
        k, op, vals = e["key"], e["operator"], e.get("values") or []
        if op == "In" and labels.get(k) not in vals:
            return False
        if op == "NotIn" and k in labels and labels[k] in vals:
            return False
        if op == "Exists" and k not in labels:
            return False
        if op == "DoesNotExist" and k in labels:
            return False
    return True


def _chain(prefix, uid):
    return f"PN-{prefix}-{hashlib.sha1(uid.encode()).hexdigest()[:12]}"


def _ports(rule, pod):
    """iptables port matches for a rule; [""] means any port."""
    out = []
    for p in rule.get("ports") or []:
        proto = (p.get("protocol") or "TCP").lower()
        port = p.get("port")
        if port is None:
            out.append(f"-p {proto}")
            continue
        if not str(port).isdigit():
            port = (
                next(
                    (
                        cp["containerPort"]
                        for c in pod["containers"]
                        for cp in c.get("ports") or []
                        if cp.get("name") == port
                    ),
                    None,
                )
                if pod
                else None
            )
            if port is None:
                continue  # a named port this pod does not have matches nothing
        end = p.get("endPort")
        out.append(f"-p {proto} --dport {port}:{end}" if end else f"-p {proto} --dport {port}")
    return out or [""]


def _peers(rule_peers, policy_ns, pods, namespaces):
    """Resolve NetworkPolicyPeers into source/destination CIDRs; None means anyone."""
    if not rule_peers:
        return None
    cidrs = []
    for peer in rule_peers:
        if "ipBlock" in peer:
            block = ipaddress.ip_network(peer["ipBlock"]["cidr"], strict=False)
            excluded = [ipaddress.ip_network(x, strict=False) for x in peer["ipBlock"].get("except") or []]
            parts = [block]
            for ex in excluded:
                parts = [q for p in parts for q in (p.address_exclude(ex) if ex.subnet_of(p) else [p])]
            cidrs += [str(p) for p in parts]
            continue
        ns_sel, pod_sel = peer.get("namespaceSelector"), peer.get("podSelector")
        for pod in pods.values():
            if ns_sel is not None:
                if not matches(ns_sel, namespaces.get(pod["namespace"], {})):
                    continue
            elif pod["namespace"] != policy_ns:
                continue
            if pod_sel is not None and not matches(pod_sel, pod["labels"]):
                continue
            cidrs.append(f"{pod['ip']}/32")
    return cidrs


def compile_rules(policies, pods, namespaces):
    """Return (iptables-restore text, set of chain names) for the current state."""
    ingress, egress = {}, {}  # pod uid -> list of (peers, ports)
    for pol in policies:
        ns, spec = pol["metadata"]["namespace"], pol["spec"]
        types = spec.get("policyTypes") or (["Ingress"] + (["Egress"] if "egress" in spec else []))
        selected = [
            uid
            for uid, p in pods.items()
            if p["namespace"] == ns and matches(spec.get("podSelector") or {}, p["labels"])
        ]
        for uid in selected:
            if "Ingress" in types:
                ingress.setdefault(uid, [])
                for r in spec.get("ingress") or []:
                    ingress[uid].append((_peers(r.get("from"), ns, pods, namespaces), _ports(r, pods[uid])))
            if "Egress" in types:
                egress.setdefault(uid, [])
                for r in spec.get("egress") or []:
                    egress[uid].append((_peers(r.get("to"), ns, pods, namespaces), _ports(r, None)))

    chains = {"PN-FWD", "PN-OUT"}
    lines = ["*filter", ":PN-FWD - [0:0]", ":PN-OUT - [0:0]"]
    body = [
        "-A PN-FWD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        f"-A PN-OUT -m mark --mark {PROBE_MARK:#x} -j RETURN",
        "-A PN-OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
    ]
    for uid, rules in egress.items():
        c = _chain("E", uid)
        chains.add(c)
        lines.append(f":{c} - [0:0]")
        body.append(f"-A PN-FWD -s {pods[uid]['ip']}/32 -j {c}")
        for peers, ports in rules:
            for dst in peers if peers is not None else [None]:
                for pm in ports:
                    body.append(f"-A {c}" + (f" -d {dst}" if dst else "") + (f" {pm}" if pm else "") + " -j RETURN")
        body.append(f"-A {c} -j DROP")
    for uid, rules in ingress.items():
        c = _chain("I", uid)
        chains.add(c)
        lines.append(f":{c} - [0:0]")
        ip = pods[uid]["ip"]
        body.append(f"-A PN-FWD -d {ip}/32 -j {c}")
        body.append(f"-A PN-OUT -d {ip}/32 -j {c}")
        for peers, ports in rules:
            for src in peers if peers is not None else [None]:
                for pm in ports:
                    body.append(f"-A {c}" + (f" -s {src}" if src else "") + (f" {pm}" if pm else "") + " -j ACCEPT")
        body.append(f"-A {c} -j DROP")
    return "\n".join(lines + body + ["COMMIT", ""]), chains


class Enforcer:
    """Applies compiled rules and garbage-collects chains of departed pods."""

    def __init__(self):
        self.chains = set()
        for parent, chain in (("FORWARD", "PN-FWD"), ("OUTPUT", "PN-OUT")):
            subprocess.run(["iptables", "-N", chain], capture_output=True)
            if subprocess.run(["iptables", "-C", parent, "-j", chain], capture_output=True).returncode:
                subprocess.run(["iptables", "-I", parent, "1", "-j", chain], check=True)

    def apply(self, policies, pods, namespaces):
        text, chains = compile_rules(policies, pods, namespaces)
        subprocess.run(["iptables-restore", "--noflush"], input=text.encode(), check=True)
        for stale in self.chains - chains:
            subprocess.run(["iptables", "-F", stale], capture_output=True)
            subprocess.run(["iptables", "-X", stale], capture_output=True)
        self.chains = chains
        return len(chains) - 2
