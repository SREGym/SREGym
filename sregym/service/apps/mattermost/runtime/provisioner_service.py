"""The provisioner's entry point as it ships in its image.

Shipped composed with ``provisioner``; only the start-up line differs.
"""
# ruff: noqa: F821 -- DATA, Server and Provisioner come from provisioner, composed in.

if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    print("provisioner listening on :8080", flush=True)
    Server(("", 8080), Provisioner).serve_forever()
