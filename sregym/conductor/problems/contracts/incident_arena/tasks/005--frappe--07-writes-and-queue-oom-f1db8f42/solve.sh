#!/usr/bin/env bash
# Golden trajectory for 07-writes-and-queue-oom (six independent faults).
# Runs inside the `main` pod, repairs all six, then files one report.
set -euo pipefail

echo "[solve] LEG 1 — MariaDB write-lock state BEFORE repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SHOW VARIABLES LIKE 'read_only';"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET GLOBAL read_only = 0;"
echo "[solve] LEG 1 — AFTER repair:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SHOW VARIABLES LIKE 'read_only';"

echo "[solve] LEGS 2-3 — restore the site account's scoped INSERT and UPDATE privileges:"
mysql --protocol=TCP -h svc-mariadb -P 3306 \
      --user=root --password="sre-world-mariadb-root" \
      -e "SET @db := (SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabDocType' AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys') LIMIT 1); SET @grantee := (SELECT GRANTEE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = @db AND PRIVILEGE_TYPE = 'SELECT' AND GRANTEE NOT LIKE \"'root'@%\" LIMIT 1); SET @sql := CONCAT('GRANT INSERT, UPDATE ON \`', @db, '\`.* TO ', @grantee); PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;"

echo "[solve] LEG 3 — redis-queue memory cap BEFORE repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$9\r\nmaxmemory\r\n');print(s.recv(4096).decode(errors='replace'))"
reconfigure-infra.sh redis-queue maxmemory 64mb
echo "[solve] LEG 3 — AFTER repair:"
python3 -c "import socket;s=socket.create_connection(('svc-redis-queue',6379),5);s.sendall(b'*3\r\n\$6\r\nCONFIG\r\n\$3\r\nGET\r\n\$9\r\nmaxmemory\r\n');print(s.recv(4096).decode(errors='replace'))"
echo "[solve] LEG 5 — restore targeted dequeue permissions:"
reconfigure-infra.sh redis-queue dequeue-acl restore

echo "[solve] LEG 6 — reconcile accepted reports and mail with RQ:"
python3 - <<'PY'
import socket
import subprocess
import zlib

def mysql(sql):
    return subprocess.check_output([
        'mysql', '--protocol=TCP', '-h', 'svc-mariadb', '-P', '3306',
        '--user=root', '--password=sre-world-mariadb-root', '-N', '-B', '-e', sql,
    ], text=True).strip()

database = mysql("SELECT TABLE_SCHEMA FROM information_schema.TABLES WHERE TABLE_NAME = 'tabPrepared Report' AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys') LIMIT 1")
if not database or '`' in database:
    raise RuntimeError('could not identify the Frappe site database')
reports = mysql(f"SELECT name FROM `{database}`.`tabPrepared Report` ORDER BY creation LIMIT 2").splitlines()
if len(reports) != 2 or reports[0] == reports[1]:
    raise RuntimeError(f'expected two distinct accepted Prepared Reports, got {reports!r}')

def redis(*parts):
    with socket.create_connection(('svc-redis-queue', 6379), 5) as conn:
        wire = b'*' + str(len(parts)).encode() + b'\r\n'
        for part in parts:
            value = part if isinstance(part, bytes) else str(part).encode()
            wire += b'$' + str(len(value)).encode() + b'\r\n' + value + b'\r\n'
        conn.sendall(wire)
        stream = conn.makefile('rb')
        def read():
            head = stream.readline()
            kind, value = head[:1], head[1:-2]
            if kind == b'-':
                raise RuntimeError('Redis command failed: ' + value.decode(errors='replace'))
            if kind == b'+':
                return value
            if kind == b':':
                return int(value)
            if kind == b'$':
                size = int(value)
                return None if size < 0 else stream.read(size + 2)[:-2]
            if kind == b'*':
                return [read() for _ in range(int(value))]
            raise RuntimeError(f'unexpected Redis response: {head!r}')
        return read()

cursor = '0'
matched = {report: [] for report in reports}
while True:
    cursor_raw, keys = redis('SCAN', cursor, 'MATCH', 'rq:job:*', 'COUNT', '100')
    for key in keys:
        data = redis('HGET', key.decode(), 'data')
        if not data:
            continue
        try:
            payload = zlib.decompress(data)
        except zlib.error:
            continue
        if b'generate_report' in payload:
            for report in reports:
                if report.encode() in payload:
                    matched[report].append(key.decode().removeprefix('rq:job:'))
    cursor = cursor_raw.decode()
    if cursor == '0':
        break
if any(len(matched[report]) != 1 for report in reports):
    raise RuntimeError(f'unexpected retained RQ jobs for accepted reports: {matched!r}')
queue = 'rq:queue:home-frappe-frappe-bench:long'
members = redis('LRANGE', queue, '0', '-1')
stranded_reports = []
for report in reports:
    job_id = matched[report][0]
    if job_id.encode() in members:
        continue
    if redis('HGET', 'rq:job:' + job_id, 'status') != b'queued':
        continue
    if redis('LPUSH', queue, job_id) < 1:
        raise RuntimeError(f'could not redeliver accepted report {report}')
    stranded_reports.append(report)
if len(stranded_reports) != 1:
    raise RuntimeError(f'expected one stranded accepted report, got {stranded_reports!r}')
print(f'redelivered accepted report {stranded_reports[0]} from its retained RQ job')

# The accepted mail is a different Frappe background method. Its four original
# jobs retain queue membership and must be delivered by the repaired worker.
mail = mysql(f"SELECT name FROM `{database}`.`tabEmail Queue` ORDER BY creation").splitlines()
if len(mail) != 4:
    raise RuntimeError(f'expected four accepted Email Queue rows, got {mail!r}')
print(f'confirmed {len(mail)} original accepted Email Queue rows')
PY

submit_incident_report \
  --service mariadb \
  --component mariadb.read-only \
  --mechanism "MariaDB's read_only global was ON, so the server refused every write from the site's application account with ERROR 1290 while serving reads normally. Restored read_only to OFF with SET GLOBAL in place." \
  --service mariadb \
  --component mariadb.grants \
  --mechanism "The site account's INSERT and UPDATE privileges were revoked, masked initially by read_only. After unlocking the server, new records and edits still failed with ERROR 1142 while reads worked. Restored only INSERT and UPDATE on the site schema to that account." \
  --service redis-queue \
  --component redis-queue.config \
  --mechanism "redis-queue had a finite 4mb cap above idle under noeviction: early jobs were accepted, then retained RQ state filled the cap and later writes failed OOM. Raised queue maxmemory to a bounded 64mb while preserving healthy AOF with everysec fsync, noeviction, and existing jobs through the protected restart without touching redis-cache." \
  --service redis-queue \
  --component redis-queue.acl \
  --mechanism "The queue user lacked BLPOP, BLMOVE and BRPOP, so the long worker could not dequeue accepted reports or mail even after capacity was raised. Reconciled one stranded Prepared Report from its retained RQ payload; the original accepted mail jobs remained queued and reached the local sink once each. Restored only the denied commands durably, preserving FLUSHALL and FLUSHDB denials and accepted jobs."

echo "[solve] incident report filed; write lock, queue capacity, and dequeue permissions repaired."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
