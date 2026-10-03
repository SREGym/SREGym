#!/usr/bin/env bash
# Golden trajectory for 07-new-records-and-queue-oom (COMPOUND: two independent faults).
# Runs inside the `main` pod. Repairs BOTH legs, then files ONE report with TWO
# findings via repeated --service/--component/--mechanism triples. Naming one
# pair only is graded "missing"; a spurious third pair is graded "extra".
set -euo pipefail

echo "[solve] LEG 1 — site INSERT grant BEFORE repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET @db := (SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys') LIMIT 1); SELECT TABLE_SCHEMA, GRANTEE, PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = @db AND PRIVILEGE_TYPE = 'INSERT' AND GRANTEE NOT LIKE \"'root'@%\";"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET @db := (SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys') LIMIT 1); SET @grantee := (SELECT GRANTEE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = @db AND PRIVILEGE_TYPE = 'SELECT' AND GRANTEE NOT LIKE \"'root'@%\" LIMIT 1); SET @sql := CONCAT('GRANT INSERT ON \`', @db, '\`.* TO ', @grantee); PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;"
echo "[solve] LEG 1 — AFTER repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET @db := (SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys') LIMIT 1); SELECT TABLE_SCHEMA, GRANTEE, PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = @db AND PRIVILEGE_TYPE = 'INSERT' AND GRANTEE NOT LIKE \"'root'@%\";"

echo "[solve] LEG 2 — redis-queue memory cap BEFORE repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$9\r\nmaxmemory\r\n');print(s.recv(4096).decode(errors='replace'))"
reconfigure-infra.sh redis-queue maxmemory 48mb
echo "[solve] LEG 2 — AFTER repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$9\r\nmaxmemory\r\n');print(s.recv(4096).decode(errors='replace'))"

submit_incident_report \
  --service mariadb \
  --component mariadb.grants \
  --mechanism "The site database account had the INSERT privilege revoked on the site schema, so creating anything new failed with ERROR 1142 while updates and reads succeeded; restored with GRANT INSERT." \
  --service redis-queue \
  --component redis-queue.config \
  --mechanism "redis-queue had a finite 3mb cap above idle under noeviction: early jobs were accepted, then retained RQ state filled the cap and later writes failed OOM. Raised only queue maxmemory to a bounded 48mb durably, preserved noeviction and existing jobs, and did not touch redis-cache."

echo "[solve] incident report filed (BOTH findings); INSERT restored, queue memory cap raised."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
