import json
import re
import uuid

import httpx
import pytest

from rotator.config import REPO_ROOT, Settings
from rotator.vault import DeviceRecord, InMemoryVault, OpenBaoVault, VaultConflict, VaultUnavailable


def openbao_vault():
    settings = Settings.from_env()
    try:
        healthy = httpx.get(f"{settings.bao_addr}/v1/sys/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        healthy = False
    if not healthy or not settings.bao_token:
        pytest.skip("OpenBao not running; start it and run scripts/bao_init.py")
    return OpenBaoVault(settings.bao_addr, settings.bao_token)


@pytest.fixture(params=["memory", pytest.param("openbao", marks=pytest.mark.openbao)])
def vault(request):
    if request.param == "memory":
        yield InMemoryVault()
        return
    vault = openbao_vault()
    yield vault
    delete_test_records(vault)


def delete_test_records(vault):
    """The rotator token can't delete device history, by design, so cleanup uses the root token."""
    root = json.loads((REPO_ROOT / ".bao" / "state.json").read_text())["root_token"]
    http = httpx.Client(base_url=str(vault.http.base_url), headers={"X-Vault-Token": root})
    for serial in vault.list_devices():
        if serial.startswith("TEST-"):
            http.delete(f"secret/metadata/devices/{serial}").raise_for_status()


def new_record(model="CoolTech CT-200"):
    serial = f"TEST-{uuid.uuid4().hex[:8]}"
    return DeviceRecord(serial=serial, model=model, name="Test", active="old", active_since="2026-01-01T00:00:00+00:00")


def test_put_and_get_round_trip(vault):
    record = new_record()
    vault.put_device(record)
    stored = vault.get_device(record.serial)
    assert stored == record and stored.version == record.version == 1
    assert record.serial in vault.list_devices()


def test_stale_write_conflicts(vault):
    record = new_record()
    vault.put_device(record)
    first, second = vault.get_device(record.serial), vault.get_device(record.serial)
    first.pending = "a"
    vault.put_device(first)
    second.pending = "b"
    with pytest.raises(VaultConflict):
        vault.put_device(second)


def test_lock_is_exclusive_and_reentrant(vault):
    serial = new_record().serial
    assert vault.acquire_lock(serial, "run-1")
    assert 0 <= vault.lock_age(serial) < 5
    assert vault.acquire_lock(serial, "run-1")
    assert not vault.acquire_lock(serial, "run-2")
    vault.release_lock(serial, "run-2")
    assert vault.lock_holder(serial) == "run-1"
    vault.release_lock(serial, "run-1")
    assert vault.lock_holder(serial) is None
    assert vault.acquire_lock(serial, "run-2")
    vault.release_lock(serial, "run-2")


def test_generated_passwords_follow_model_rules(vault):
    for _ in range(5):
        ct = vault.generate_password("CoolTech CT-200")
        assert re.fullmatch(r"[A-Za-z0-9!@#%]{16}", ct)
        assert re.search(r"[a-z]", ct) and re.search(r"[A-Z]", ct) and re.search(r"\d", ct) and re.search(r"[!@#%]", ct)
        assert re.fullmatch(r"\d{8}", vault.generate_password("FrostLine FL-9"))


def test_memory_vault_can_fail_writes():
    vault = InMemoryVault(fail_writes=1)
    with pytest.raises(VaultUnavailable):
        vault.put_device(new_record())


def test_record_repr_hides_passwords():
    record = new_record()
    record.pending = "s3cret-pending"
    assert "old" not in repr(record) and "s3cret" not in repr(record)
