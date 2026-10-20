# ruff: noqa: I001 -- neutron.cmd.eventlet monkey-patches on import, so it must come first.
"""Run neutron-server with its ORM mappers configured before the API workers fork.

With no ML2 mechanism drivers nothing queries the database during start-up, so
SQLAlchemy has not yet attached backrefs such as ``Port.port_forwardings``
when the first ``GET /v2.0/ports`` reads them, and that request fails.
Configuring the mappers once the service plugins have loaded avoids it.
"""

import sys

from neutron.cmd.eventlet.server import main
from neutron import manager
from sqlalchemy import orm

_load_service_plugins = manager.NeutronManager._load_service_plugins


def _load_and_configure(self):
    _load_service_plugins(self)
    orm.configure_mappers()


manager.NeutronManager._load_service_plugins = _load_and_configure

if __name__ == "__main__":
    sys.argv[0] = "neutron-server"
    sys.exit(main())
