# ruff: noqa: UP032 -- This helper also runs under the application's Python 2.7.
import os
import signal

wanted = []
for p in os.listdir("/proc"):
    if not p.isdigit() or int(p) in (1, os.getpid()):
        continue
    try:
        with open("/proc/" + p + "/environ", "rb") as stream:
            env = stream.read().split(b"\0")
        if b"REFRESH_TARGET=nginx-thrift" in env:
            wanted.append(int(p))
    except IOError:  # noqa: UP024 -- Python 2.7 distinguishes IOError from OSError.
        continue
for pid in wanted:
    try:  # noqa: SIM105 -- The application Python 2.7 lacks contextlib.suppress.
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
# Ensure a concurrent publication finishes before configuration is repaired.
for pid in wanted:
    try:  # noqa: SIM105 -- The application Python 2.7 lacks contextlib.suppress.
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
print("Stopped {} configuration publishers".format(len(wanted)))
