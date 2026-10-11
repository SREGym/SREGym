#!/usr/bin/env bash
flush_profiles() {
 kubectl -n "$NAMESPACE" exec deploy/profile -- python3 -c 'import socket; s=socket.create_connection(("memcached-profile",11211),5); s.sendall(b"flush_all\r\n"); r=s.recv(128); s.close(); assert r.startswith(b"OK"),r'
}
