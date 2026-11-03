"""Initialise, unseal and configure OpenBao. Safe to run again: it only unseals if already set up.

Writes the unseal key, root token and rotator token to .bao/state.json (Git-ignored). A production setup
would use auto-unseal with a cloud KMS and revoke the root token instead.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rotator.vault import POLICIES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".bao" / "state.json"

ROTATOR_ACL = """
path "secret/data/devices/*"        { capabilities = ["create", "read", "update"] }
path "secret/metadata/devices"      { capabilities = ["list"] }
path "secret/data/locks/*"          { capabilities = ["create", "read", "update"] }
path "secret/metadata/locks/*"      { capabilities = ["read", "delete"] }
path "secret/data/dashboard/admin"  { capabilities = ["read"] }
path "sys/policies/password/ct200/generate" { capabilities = ["read"] }
path "sys/policies/password/fl9/generate"   { capabilities = ["read"] }
"""


def main() -> None:
    addr = os.environ.get("BAO_ADDR", "http://127.0.0.1:8200").rstrip("/") + "/v1"
    http = httpx.Client(base_url=addr, timeout=10)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    try:
        status = http.get("sys/seal-status").json()
    except httpx.ConnectError:
        raise SystemExit(f"OpenBao is not running at {addr}. Start it first, in its own terminal: "
                         "./scripts/bao_server.sh (or docker compose up -d)") from None
    if not status["initialized"]:
        init = http.put("sys/init", json={"secret_shares": 1, "secret_threshold": 1}).raise_for_status().json()
        state = {"unseal_key": (init.get("keys_base64") or init["keys"])[0], "root_token": init["root_token"]}
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2))
        STATE.chmod(0o600)
        print("Initialised OpenBao.")
    elif not state:
        sys.exit("OpenBao is initialised but .bao/state.json is missing. Restore it or reset the OpenBao data.")

    if http.get("sys/seal-status").json()["sealed"]:
        http.put("sys/unseal", json={"key": state["unseal_key"]}).raise_for_status()
        print("Unsealed.")
    # After unseal, Raft needs a moment to elect this node leader; writes fail until then.
    for _ in range(50):
        if http.get("sys/health").status_code == 200:
            break
        time.sleep(0.2)

    http.headers["X-Vault-Token"] = state["root_token"]
    if "secret/" not in http.get("sys/mounts").raise_for_status().json()["data"]:
        http.post("sys/mounts/secret", json={"type": "kv", "options": {"version": "2"}}).raise_for_status()
        print("Enabled KV v2 at secret/.")
    for name, policy in POLICIES.items():
        http.put(f"sys/policies/password/{name}", json={"policy": policy}).raise_for_status()
    http.put("sys/policies/acl/rotator", json={"policy": ROTATOR_ACL}).raise_for_status()

    if "rotator_token" not in state:
        token = http.post("auth/token/create-orphan", json={"policies": ["rotator"], "ttl": "8760h"}).raise_for_status()
        state["rotator_token"] = token.json()["auth"]["client_token"]
        STATE.write_text(json.dumps(state, indent=2))
        print("Created rotator token.")
    print("OpenBao ready.")


if __name__ == "__main__":
    main()
