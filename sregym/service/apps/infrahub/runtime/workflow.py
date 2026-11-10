"""Probes run in the application-client pod, appended after infrahub_client.py (whose names they use)."""
# ruff: noqa: F821

import time
import uuid


def _wait_views(c, expected, timeout, samples=24):
    """Wait until every sampled answer lists exactly ``expected[kind]`` for every kind."""
    want = {kind: ",".join(sorted(attrs)) for kind, attrs in expected.items()}
    deadline = time.monotonic() + timeout
    views = {}
    while time.monotonic() < deadline:
        try:
            views = {kind: c.attribute_views(samples, kind) for kind in want}
        except Exception as exc:
            views = {"error": str(exc)[:200]}
        if all(list(views.get(kind, {})) == [w] for kind, w in want.items()):
            return True, views
        time.sleep(3)
    return False, views


def _retry_busy(fn, *args, attempts=20, **kwargs):
    """Retry a write that Infrahub refused because a merge into main was running (its retryable error)."""
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except InfrahubError as exc:
            if "retry shortly" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(3)


def _base_expected():
    return {kind: ["name", *fields] for kind, fields in BASE_FIELDS.items()}


def _history(c, kw, device_extra):
    """Older branches (the network team's plans, the teams' schema work) and a later schema release on main."""
    created = []
    for i in range(kw.get("history_branches", 0)):
        branch = f"plan-{i + 1:03d}"
        if c.branch_status(branch) is None:
            _retry_busy(c.branch_create, branch)
            _retry_busy(c.create_devices, {f"dev-{branch}": {"role": "leaf", "site": "dc2"}}, branch=branch)
        created.append(branch)
    teams = {}
    plan = list((kw.get("team_branches") or {}).items())
    for after_release in (False, True):
        if after_release and kw.get("main_release"):
            status, body = c.load_schema([device_node(extra=list(device_extra) + kw["main_release"])])
            if status != 200:
                raise InfrahubError(f"main schema release: HTTP {status}: {body}")
        for branch, team in plan:
            if bool(team.get("after_release")) != after_release:
                continue
            if c.branch_status(branch) is None:
                _retry_busy(c.branch_create, branch)
                status, body = c.load_schema([device_node(extra=list(device_extra) + team["attrs"])], branch=branch)
                if status != 200:
                    raise InfrahubError(f"schema load on {branch}: HTTP {status}: {body}")
                names = {f"{team['prefix']}-{i:02d}": {"role": "leaf", "site": "dc1"} for i in range(team["devices"])}
                _retry_busy(c.create_devices, names, branch=branch)
                if team.get("merge"):
                    task = (_retry_busy(c.merge_submit, branch).get("task") or {}).get("id")
                    took, error = c.wait_merged(branch, 600, task)
                    if took is None:
                        raise InfrahubError(f"merge of {branch}: {error}")
            teams[branch] = team
    return {"history_branches": created, "team_branches": sorted(teams)}


def workflow(c, mode, **kw):
    if mode == "bootstrap":
        device_extra = kw.get("device_extra", [])
        status, body = c.load_schema([device_node(extra=device_extra, dropdowns=kw.get("dropdowns")), module_node()])
        if status != 200:
            raise InfrahubError(f"base schema load: HTTP {status}: {body}")
        expected = _base_expected()
        expected[DEVICE_KIND] = sorted(expected[DEVICE_KIND] + list(device_extra))
        ok, views = _wait_views(c, expected, 300)
        if not ok:
            raise InfrahubError(f"API workers did not converge on the base schema: {views}")
        seed = kw.get("seed_values") or {}
        devices, modules = [], []
        for i in range(kw.get("devices", 24)):
            name = f"core-{i:03d}"
            if c.get_device(name) is None:
                fields = {"role": "core" if i % 4 == 0 else "leaf", "site": f"dc{i % 3 + 1}"}
                fields.update({f: values[i % len(values)] for f, values in seed.items()})
                c.create_device(name, fields)
            if c.get(MODULE_KIND, f"{name}-m1", ("device",)) is None:
                c.create(MODULE_KIND, f"{name}-m1", {"device": name, "slot": "1"})
            devices.append(name)
            modules.append(f"{name}-m1")
        history = _history(c, kw, device_extra)
        return {"seeded": devices, "modules": modules, "views": views, **history}
    if mode == "choice_views":
        return c.choice_views(kw.get("samples", 24))
    if mode == "dropdown_release":
        return c.dropdown_add_concurrently([tuple(change) for change in kw["changes"]], kw.get("gap", 0.3))
    if mode == "dropdown_add":
        try:
            return {"ok": bool(c.dropdown_add(kw["attribute"], kw["choice"], label=kw.get("label"))["ok"])}
        except InfrahubError as exc:
            return {"ok": False, "error": str(exc)[:300]}
    if mode == "probe_choices":
        # One device per wanted value, created and read back over fresh connections.
        results = []
        for field, values in kw["values"].items():
            for value in values:
                name = f"probe-{uuid.uuid4().hex[:10]}"
                fields = {"role": "leaf", **kw.get("fixed", {}), field: value}
                try:
                    c.create_device(name, fields)
                    seen = c.get_device(name, fields=tuple(fields))
                    ok = bool(seen) and all(seen.get(f) == v for f, v in fields.items())
                    results.append({"field": field, "value": value, "ok": ok, "seen": seen})
                except Exception as exc:
                    results.append({"field": field, "value": value, "ok": False, "error": str(exc)[:300]})
        return {"ok": all(r["ok"] for r in results), "results": results}
    if mode == "views":
        return {kind: c.attribute_views(kw.get("samples", 24), kind) for kind in kw.get("kinds", [DEVICE_KIND])}
    if mode == "wait_views":
        ok, views = _wait_views(c, kw["expected"], kw.get("timeout", 120), kw.get("samples", 24))
        return {"converged": ok, "views": views}
    if mode == "release":
        files = [[{"device": device_node, "module": module_node}[k](extra=v) for k, v in f] for f in kw["files"]]
        return c.load_concurrently(files, kw.get("gap", 0.3))
    if mode == "load_schema":
        nodes = [device_node(extra=kw.get("extra", ()), absent=kw.get("absent", ()))]
        if kw.get("module_extra") is not None:
            nodes.append(module_node(extra=kw["module_extra"]))
        started = time.time()
        status, body = c.load_schema(nodes)
        return {
            "status": status,
            "seconds": round(time.time() - started, 2),
            "error": None if status == 200 else str(body)[:500],
        }
    if mode == "probe_objects":
        results = []
        for i in range(kw.get("count", 4)):
            device = f"probe-{uuid.uuid4().hex[:10]}"
            for kind, name, fields in (
                (DEVICE_KIND, device, kw["device_fields"]),
                (MODULE_KIND, f"{device}-m1", kw["module_fields"]),
            ):
                values = {f: (device if f == "device" else f"{f}-{i}") for f in fields}
                try:
                    c.create(kind, name, values)
                    seen = c.get(kind, name, tuple(fields))
                    ok = bool(seen) and all(seen.get(f) == v for f, v in values.items())
                    results.append({"kind": kind, "name": name, "ok": ok, "seen": seen})
                except Exception as exc:
                    results.append({"kind": kind, "name": name, "ok": False, "error": str(exc)[:300]})
        return {"ok": all(r["ok"] for r in results), "results": results}
    if mode == "probe_devices":
        fields = kw["fields"]
        results = []
        for i in range(kw.get("count", 4)):
            name = f"probe-{uuid.uuid4().hex[:10]}"
            values = {f: f"{f}-{i}" for f in fields}
            try:
                c.create_device(name, values)
                seen = c.get_device(name, fields=tuple(fields))
                ok = bool(seen) and all(seen.get(f) == v for f, v in values.items())
                results.append({"device": name, "ok": ok, "seen": seen})
            except Exception as exc:
                results.append({"device": name, "ok": False, "error": str(exc)[:300]})
        return {"ok": all(r["ok"] for r in results), "results": results}
    if mode == "inventory":
        limit = kw.get("limit", 5000)
        return {
            "count": c.count(DEVICE_KIND),
            # A device vertex without its name attribute (a partial merge) has no name to list.
            "names": sorted(n for n in c.names(DEVICE_KIND, limit=limit) if n),
            "modules": sorted(n for n in c.names(MODULE_KIND, limit=limit) if n),
        }
    if mode == "missing_devices":
        return {"missing": c.missing(kw["names"], kw.get("kind", DEVICE_KIND))}
    if mode == "touch_schema":
        node = device_node(extra=kw.get("extra", ()))
        node["description"] = kw["description"]
        started = time.time()
        for _ in range(20):
            status, body = c.load_schema([node], timeout=kw.get("timeout", 180))
            if status == 200 or "retry shortly" not in str(body):
                break
            time.sleep(3)
        return {
            "status": status,
            "seconds": round(time.time() - started, 1),
            "error": None if status == 200 else str(body)[:300],
        }
    if mode == "branches":
        return c.branches()
    if mode == "branch_change":
        branch = kw.get("branch") or f"release-{uuid.uuid4().hex[:8]}"
        started = time.monotonic()
        _retry_busy(c.branch_create, branch, timeout=kw.get("timeout", 300))
        _retry_busy(
            c.create_devices,
            {f"dev-{branch}-{i:04d}": {"role": "leaf", "site": "dc2"} for i in range(kw.get("devices", 1))},
            branch=branch,
        )
        if not kw.get("merge", True):
            return {"branch": branch, "devices": kw.get("devices", 1)}
        task = (_retry_busy(c.merge_submit, branch).get("task") or {}).get("id")
        took, error = c.wait_merged(branch, kw.get("timeout", 300), task)
        return {
            "branch": branch,
            "merged": took is not None,
            "error": error,
            "seconds": round(time.monotonic() - started, 1),
        }
    if mode == "release_pipeline":
        # A team's release: load the rest of its schema change into its branch, then merge it.
        started = time.time()
        status, body = c.load_schema([device_node(extra=kw["attrs"])], branch=kw["branch"])
        loaded = round(time.time() - started, 1)
        if status != 200:
            return {"loaded": False, "status": status, "error": str(body)[:300]}
        task = (_retry_busy(c.merge_submit, kw["branch"]).get("task") or {}).get("id")
        took, error = c.wait_merged(kw["branch"], kw.get("timeout", 300), task)
        return {
            "loaded": True,
            "load_seconds": loaded,
            "merged": took is not None,
            "error": error,
            "seconds": round(time.time() - started, 1),
        }
    if mode == "kind_attributes":
        return {"attributes": c.kind_attributes(kw.get("kind", DEVICE_KIND), branch=kw.get("branch", "main"))}
    if mode == "merge":
        task = (c.merge_submit(kw["branch"]).get("task") or {}).get("id")
        if kw.get("wait", 0):
            took, error = c.wait_merged(kw["branch"], kw["wait"], task)
            return {"branch": kw["branch"], "merged": took is not None, "seconds": took, "error": error}
        return {"branch": kw["branch"], "submitted": True, "task": task}
    if mode == "branch_status":
        return {"branch": kw["branch"], "status": c.branch_status(kw["branch"])}
    raise ValueError(f"unknown mode {mode}")
