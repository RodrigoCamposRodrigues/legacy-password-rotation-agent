"""Fresh demo data: dashboard devices and admin login, with the same passwords in OpenBao.

Uses the root token, because the rotator's own token may not write the dashboard admin credential.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from demo_seed import new_admin, seed_all  # noqa: E402
from rotator.config import Settings  # noqa: E402
from rotator.vault import OpenBaoVault  # noqa: E402
from simclock import SystemClock  # noqa: E402

if __name__ == "__main__":
    settings = Settings.from_env()
    root_token = json.loads((ROOT / ".bao" / "state.json").read_text())["root_token"]
    vault = OpenBaoVault(settings.bao_addr, root_token)
    admin = new_admin()
    vault.put_admin_credential(*admin)
    db_path = Path(os.environ.get("DASHBOARD_DB", ROOT / "data" / "dashboard.db"))
    seed_all(db_path, vault, SystemClock(settings.clock_offset_days), admin)
    print(f"Seeded {len(vault.list_devices())} devices into {db_path} and OpenBao.")
