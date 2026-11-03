"""Checks every device: does the password the vault calls `active` log in to the device right now?

This is the system's main promise, so run it after any test. Exit code 1 if any device is broken.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotator.config import Settings  # noqa: E402
from rotator.device import DeviceProbe  # noqa: E402
from rotator.vault import OpenBaoVault  # noqa: E402

if __name__ == "__main__":
    settings = Settings.from_env()
    vault = OpenBaoVault(settings.bao_addr, settings.bao_token)
    probe = DeviceProbe(settings.dashboard_url)
    broken = 0
    for serial in vault.list_devices():
        record = vault.get_device(serial)
        works = probe.check(serial, record.active)
        broken += not works
        lock = vault.lock_holder(serial) or "-"
        print(f"{serial:12} {'OK    ' if works else 'BROKEN'} state={record.state:8} since={record.active_since[:10]} lock={lock}")
    print(f"\n{len(vault.list_devices()) - broken} OK, {broken} broken")
    sys.exit(1 if broken else 0)
