#!/usr/bin/env bash
# Golden trajectory for 07-desk-and-queue-oom (COMPOUND: two independent faults).
# Runs inside the `main` pod. Repairs BOTH legs, then files ONE report with TWO
# findings via repeated --service/--component/--mechanism triples. Naming one
# pair only is graded "missing"; a spurious third pair is graded "extra".
set -euo pipefail

echo "[solve] LEG 1 — MariaDB per-account ceiling BEFORE repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SHOW VARIABLES LIKE 'max_user_connections'; \
          SHOW VARIABLES LIKE 'max_connections';"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET GLOBAL max_user_connections = 0;"
echo "[solve] LEG 1 — AFTER repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SHOW VARIABLES LIKE 'max_user_connections';"

echo "[solve] LEG 2 — redis-queue memory cap BEFORE repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$9\r\nmaxmemory\r\n');print(s.recv(4096).decode(errors='replace'))"
reconfigure-infra.sh redis-queue maxmemory 32mb
echo "[solve] LEG 2 — AFTER repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$9\r\nmaxmemory\r\n');print(s.recv(4096).decode(errors='replace'))"

submit_incident_report \
  --service mariadb \
  --component mariadb.max-user-connections \
  --mechanism "MariaDB's per-account ceiling max_user_connections was 8, equal to the steady-state Desk demand of the site's application account. Frappe opens a connection per request and has no app-side pool, so peak concurrency crossed the per-account ceiling and MariaDB refused further connects for that account with error 1226, surfacing as 500s on the Desk API while the server-wide max_connections ceiling was never approached. Restored max_user_connections to the documented server default of 0 (unlimited)." \
  --service redis-queue \
  --component redis-queue.config \
  --mechanism "redis-queue had a finite 2mb cap above idle under noeviction: early jobs were accepted, then retained RQ state filled the cap and later writes failed OOM. Raised only queue maxmemory to a bounded 32mb durably, preserved noeviction and existing jobs, and did not touch redis-cache."

echo "[solve] incident report filed (BOTH findings); per-account ceiling restored, queue memory cap raised."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
