#!/usr/bin/env bash
# Golden trajectory for 07-desk-and-queue-outage (COMPOUND: two independent faults).
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

echo "[solve] LEG 2 — redis-queue write guard BEFORE repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$21\r\nmin-replicas-to-write\r\n');print(s.recv(4096).decode(errors='replace'))"
reconfigure-infra.sh redis-queue min-replicas-to-write 0
echo "[solve] LEG 2 — AFTER repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$21\r\nmin-replicas-to-write\r\n');print(s.recv(4096).decode(errors='replace'))"

submit_incident_report \
  --service mariadb \
  --component mariadb.max-user-connections \
  --mechanism "MariaDB's per-account ceiling max_user_connections was 8, equal to the steady-state Desk demand of the site's application account. Frappe opens a connection per request and has no app-side pool, so peak concurrency crossed the per-account ceiling and MariaDB refused further connects for that account with error 1226, surfacing as 500s on the Desk API while the server-wide max_connections ceiling was never approached. Restored max_user_connections to the documented server default of 0 (unlimited)." \
  --service redis-queue \
  --component redis-queue.config \
  --mechanism "redis-queue was started with --min-replicas-to-write 1 on a standalone master. With zero replicas the replication guard can never be satisfied, so redis refused every write with NOREPLICAS; reads and PING stayed green while every frappe.enqueue() returned 503. Set min-replicas-to-write back to 0 on svc-redis-queue via reconfigure-infra.sh; did not touch min-replicas-max-lag or the topology."

echo "[solve] incident report filed (BOTH findings); per-account ceiling restored, queue write guard cleared."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
