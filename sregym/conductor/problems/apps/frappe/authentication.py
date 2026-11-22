"""Frappe: authentication."""

from __future__ import annotations

import json

from sregym.conductor.oracles.apps.frappe.state import FrappeFaultStateOracle
from sregym.conductor.problems.apps.frappe.helpers import (
    APP,
    GUNICORN,
    delete_pods,
    site_config,
    site_db_probe,
    state_file,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.service.apps.frappe.operations import schema_privileges, site_account, site_database
from sregym.utils.decorators import mark_fault_injected


class _SiteAccountFault(Problem):
    """Base for faults on the Frappe site's MariaDB account (was a MongoDB user)."""

    DESCRIPTION = ""
    COMPONENT = "statefulset/frappe-mariadb-subchart"
    # Faithful to the originals, which restarted the dependent service so it reconnects into the fault.
    RESTART_CLIENTS = (GUNICORN,)

    def __init__(self, app_name: str = APP):
        super().__init__(app=make_app(app_name))
        self.faulty_service = "frappe-mariadb-subchart"
        self.database = ""
        self.grantee = ""
        configure_port(
            self,
            app_name,
            component=self.COMPONENT,
            description=self.DESCRIPTION,
            oracle_factory=FrappeFaultStateOracle,
        )

    def _resolve(self) -> None:
        if self.grantee:
            return
        try:
            self.database = site_database(self.app)
            self.grantee = site_account(self.app, self.database)
        except RuntimeError:
            # Under the fault the account may hold no privilege (or not exist): use what inject saved.
            saved = self._load()
            if not saved.get("grantee"):
                raise
            self.database, self.grantee = saved["database"], saved["grantee"]

    def _save(self, **extra) -> None:
        state_file(self, type(self).__name__).write_text(
            json.dumps({"database": self.database, "grantee": self.grantee, **extra})
        )

    def _load(self) -> dict:
        path = state_file(self, type(self).__name__)
        return json.loads(path.read_text()) if path.exists() else {}

    def _restart_clients(self) -> None:
        for deployment in self.RESTART_CLIENTS:
            print(f"Deleted {deployment} pods {delete_pods(self, deployment)} so they reconnect")

    def fault_check(self, oracle) -> dict | None:
        return site_db_probe(self, oracle)


class RevokeAuthFrappe(_SiteAccountFault):
    """``REVOKE SELECT, INSERT, UPDATE, DELETE`` on the site schema from the site account."""

    PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
    COMPONENT = "statefulset/frappe-mariadb-subchart (grants of the Frappe site account)"
    DESCRIPTION = (
        "Database access for the Frappe site was explicitly revoked in MariaDB (`frappe-mariadb-subchart`): "
        "`REVOKE SELECT, INSERT, UPDATE, DELETE ON <site schema>.* FROM <site account>`, where the site account "
        "is the per-site user named in `sites/svc-frappe-web/site_config.json` (`db_name`). The account still "
        "exists and authenticates, so gunicorn (`erp-gunicorn`, Service `svc-frappe-web`) and the RQ workers "
        "start, but every database-backed request fails with ERROR 1142 (`SELECT command denied to user ...`), "
        "so the Desk/API requests of the site's users fail. Fix: grant the privileges back to the site account."
    )

    @mark_fault_injected
    def inject_fault(self):
        self._resolve()
        self._save()
        self.app.mysql(f"REVOKE {', '.join(self.PRIVILEGES)} ON `{self.database}`.* FROM {self.grantee}")
        held = set(schema_privileges(self.app, self.database, self.grantee))
        if held & set(self.PRIVILEGES):
            raise RuntimeError(f"{self.grantee} still holds {sorted(held & set(self.PRIVILEGES))}")
        print(f"Revoked {self.PRIVILEGES} on {self.database} from {self.grantee}")
        self._restart_clients()

    @mark_fault_injected
    def recover_fault(self):
        self._resolve()
        self.app.mysql(f"GRANT {', '.join(self.PRIVILEGES)} ON `{self.database}`.* TO {self.grantee}")
        print(f"Granted {self.PRIVILEGES} on {self.database} back to {self.grantee}")


class StorageUserUnregisteredFrappe(_SiteAccountFault):
    """``DROP USER`` of the site's MariaDB account; gunicorn is restarted into the failure."""

    COMPONENT = "statefulset/frappe-mariadb-subchart (Frappe site account)"
    DESCRIPTION = (
        "The Frappe site's MariaDB account is missing: the per-site user (named by `db_name` in "
        "`sites/svc-frappe-web/site_config.json`, with password `db_password`) was dropped from MariaDB "
        "(`frappe-mariadb-subchart`). Gunicorn (`erp-gunicorn`, Service `svc-frappe-web`) and the RQ workers "
        "cannot authenticate (`Access denied for user ...`, ERROR 1045), so every storage-backed request fails. "
        "Fix: recreate the user with the site's password and grant it all privileges on the site schema."
    )

    @mark_fault_injected
    def inject_fault(self):
        self._resolve()
        grants = [line for line in self.app.mysql(f"SHOW GRANTS FOR {self.grantee}").splitlines() if line.strip()]
        self._save(grants=grants)
        self.app.mysql(f"DROP USER {self.grantee}")
        print(f"Dropped {self.grantee} (had {len(grants)} grant lines)")
        self._restart_clients()

    @mark_fault_injected
    def recover_fault(self):
        self._resolve()
        grants = self._load().get("grants") or []
        if not grants:
            raise RuntimeError("no saved grants for the site account")
        config = site_config(self)
        password = config["db_password"].replace("'", "''")
        self.app.mysql(f"CREATE USER IF NOT EXISTS {self.grantee} IDENTIFIED BY '{password}'")
        for line in grants:
            if line.upper().startswith("GRANT USAGE ON *.*"):
                continue
            self.app.mysql(line)
        print(f"Recreated {self.grantee} with {len(grants)} grant lines")


class AuthMissFrappe(_SiteAccountFault):
    """MariaDB demands TLS (``require_secure_transport=ON``) that none of its clients use."""

    COMPONENT = "statefulset/frappe-mariadb-subchart (require_secure_transport)"
    DESCRIPTION = (
        "The Frappe MariaDB server (`frappe-mariadb-subchart`) requires secure transport: the global "
        "`require_secure_transport` was set to ON, but TLS is not set up between the server and its clients "
        "(Frappe connects over plain TCP and no client certificate/TLS configuration exists). Every TCP connection "
        "is refused at authentication (`Access denied for user '<site account>'@'<pod IP>' (using password: YES)`, "
        "ERROR 1045, although the password in site_config.json is correct), "
        "so gunicorn (`erp-gunicorn`) and the RQ workers cannot reach the database and every database-dependent "
        "request fails. Fix: turn `require_secure_transport` off (or set up working TLS for every client)."
    )

    @mark_fault_injected
    def inject_fault(self):
        self.app.mysql("SET GLOBAL require_secure_transport = ON")
        print("MariaDB now rejects non-TLS connections")
        self._restart_clients()

    @mark_fault_injected
    def recover_fault(self):
        self.app.mysql("SET GLOBAL require_secure_transport = OFF")
        print("MariaDB accepts plain-TCP clients again")
