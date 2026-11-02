"""Put the same starting passwords in the dashboard and the vault, as staff would on day one."""

import secrets
from datetime import timedelta
from pathlib import Path

from dashboard.db import DEFAULT_DEVICES, seed
from rotator.vault import DeviceRecord, Vault
from simclock import Clock


def seed_all(db_path: Path, vault: Vault, clock: Clock, admin: tuple[str, str]) -> dict[str, str]:
    """Returns serial -> starting password, for tests and evals only."""
    now = clock.now()
    passwords = {serial: vault.generate_password(model) for _, model, serial, *_ in DEFAULT_DEVICES}
    seed(db_path, passwords, admin, now)
    for name, model, serial, _, age_days, _ in DEFAULT_DEVICES:
        try:
            record = vault.get_device(serial)
        except KeyError:
            record = DeviceRecord(serial=serial, model=model, name=name, active="", active_since="")
        record.model, record.name = model, name
        record.active, record.pending, record.pending_run_id, record.previous = passwords[serial], "", "", ""
        record.state = "active"
        record.active_since = (now - timedelta(days=age_days)).isoformat()
        vault.put_device(record)
    return passwords


def new_admin() -> tuple[str, str]:
    return "admin", secrets.token_urlsafe(12)
