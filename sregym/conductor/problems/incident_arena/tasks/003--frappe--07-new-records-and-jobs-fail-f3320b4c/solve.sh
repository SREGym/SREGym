#!/usr/bin/env bash
# Golden trajectory for 07-new-records-and-jobs-fail (COMPOUND: two independent faults).
# Runs inside the `main` pod. Repairs BOTH legs, then files ONE report with TWO
# findings via repeated --service/--component/--mechanism triples. Naming one
# pair only is graded "missing"; a spurious third pair is graded "extra".
#
# Leg 1 is a masquerade: the presentation is the read-only lock, but read_only
# is OFF and stays OFF. The site schema and its grantee are discovered from
# information_schema (nothing hard-coded); GRANT takes effect on the next
# statement. Deliberately NOT done: SET GLOBAL read_only = 0 (already 0, and a
# declared probe that must not move), restarting MariaDB (a grant survives a
# restart; restart masking fails services_up), touching any connection ceiling.
set -euo pipefail

echo "[solve] LEG 1 — MariaDB state BEFORE repair (read_only is OFF; the grant is the cause):"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SHOW VARIABLES LIKE 'read_only'; \
          SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES \
           WHERE TABLE_SCHEMA = (SELECT TABLE_SCHEMA FROM information_schema.TABLES \
                                  WHERE TABLE_NAME = 'tabDocType' LIMIT 1) \
           ORDER BY GRANTEE, PRIVILEGE_TYPE;"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET @db := (SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys') LIMIT 1); SET @grantee := (SELECT GRANTEE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = @db AND PRIVILEGE_TYPE = 'SELECT' AND GRANTEE NOT LIKE \"'root'@%\" LIMIT 1); SET @sql := CONCAT('GRANT INSERT ON \`', @db, '\`.* TO ', @grantee); PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;"
echo "[solve] LEG 1 — AFTER repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES \
           WHERE TABLE_SCHEMA = (SELECT TABLE_SCHEMA FROM information_schema.TABLES \
                                  WHERE TABLE_NAME = 'tabDocType' LIMIT 1) \
           ORDER BY GRANTEE, PRIVILEGE_TYPE;"

echo "[solve] LEG 2 — restore only the queue dequeue commands and persist them:"
reconfigure-infra.sh redis-queue dequeue-acl restore

submit_incident_report \
  --service mariadb \
  --component mariadb.grants \
  --mechanism "The site database account had the INSERT privilege revoked on the site schema, so creating anything new failed with ERROR 1142 (INSERT command denied to user) while updates, reads and connectivity succeeded; read_only was OFF and no server global had moved, which rules out the read-only lock. Restored with GRANT INSERT ON the site schema to the site account, no restart, no global touched." \
  --service redis-queue \
  --component redis-queue.acl \
  --mechanism "Jobs were accepted into redis-queue, but its default user lacked BLPOP, BLMOVE and BRPOP, so the worker received NOPERM and jobs remained Queued. Restored only those dequeue commands durably; did not flush data, scale workers, or alter redis-cache."

echo "[solve] incident report filed (BOTH findings); INSERT privilege restored, queue write guard released."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
