"""Password storage. The vault is the source of truth: a password exists here before any device uses it."""

import secrets
import string
from dataclasses import asdict, dataclass, field
from typing import Protocol

import httpx

MODEL_POLICY = {"CoolTech CT-200": "ct200", "FrostLine FL-9": "fl9"}

POLICIES = {
    "ct200": """
length = 16
rule "charset" {
  charset = "abcdefghijklmnopqrstuvwxyz"
  min-chars = 1
}
rule "charset" {
  charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
  min-chars = 1
}
rule "charset" {
  charset = "0123456789"
  min-chars = 1
}
rule "charset" {
  charset = "!@#%"
  min-chars = 1
}
""",
    "fl9": """
length = 8
rule "charset" {
  charset = "0123456789"
  min-chars = 8
}
""",
}


class VaultError(Exception):
    pass


class VaultConflict(VaultError):
    """Someone else wrote the record since we read it."""


class VaultUnavailable(VaultError):
    pass


@dataclass
class DeviceRecord:
    serial: str
    model: str
    name: str
    active: str
    active_since: str
    state: str = "active"
    pending: str = ""
    pending_run_id: str = ""
    previous: str = ""
    version: int = field(default=0, compare=False)

    def __repr__(self) -> str:
        # Keeps passwords out of tracebacks and logs if a record is ever printed.
        return f"DeviceRecord(serial={self.serial!r}, state={self.state!r}, pending_run_id={self.pending_run_id!r}, version={self.version})"

    def data(self) -> dict:
        values = asdict(self)
        values.pop("version")
        return values


class Vault(Protocol):
    def get_device(self, serial: str) -> DeviceRecord: ...
    def put_device(self, record: DeviceRecord) -> None: ...
    def list_devices(self) -> list[str]: ...
    def acquire_lock(self, serial: str, run_id: str) -> bool: ...
    def lock_holder(self, serial: str) -> str | None: ...
    def release_lock(self, serial: str, run_id: str) -> None: ...
    def generate_password(self, model: str) -> str: ...
    def admin_credential(self) -> tuple[str, str]: ...


def generate_locally(model: str) -> str:
    policy = MODEL_POLICY[model]
    if policy == "fl9":
        return "".join(secrets.choice(string.digits) for _ in range(8))
    sets = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#%"]
    chars = [secrets.choice(s) for s in sets] + [secrets.choice("".join(sets)) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class InMemoryVault:
    """Test double with the same check-and-set and lock semantics as the OpenBao layout."""

    def __init__(self, admin: tuple[str, str] = ("admin", "admin"), fail_writes: int = 0):
        self.records: dict[str, tuple[int, dict]] = {}
        self.locks: dict[str, str] = {}
        self.admin = admin
        self.fail_writes = fail_writes

    def get_device(self, serial: str) -> DeviceRecord:
        version, data = self.records[serial]
        return DeviceRecord(**data, version=version)

    def put_device(self, record: DeviceRecord) -> None:
        if self.fail_writes:
            self.fail_writes -= 1
            raise VaultUnavailable("simulated vault outage")
        current = self.records.get(record.serial, (0, None))[0]
        if current != record.version:
            raise VaultConflict(f"{record.serial}: expected version {record.version}, found {current}")
        self.records[record.serial] = (current + 1, record.data())
        record.version = current + 1

    def list_devices(self) -> list[str]:
        return sorted(self.records)

    def acquire_lock(self, serial: str, run_id: str) -> bool:
        holder = self.locks.setdefault(serial, run_id)
        return holder == run_id

    def lock_holder(self, serial: str) -> str | None:
        return self.locks.get(serial)

    def release_lock(self, serial: str, run_id: str) -> None:
        if self.locks.get(serial) == run_id:
            del self.locks[serial]

    def generate_password(self, model: str) -> str:
        return generate_locally(model)

    def admin_credential(self) -> tuple[str, str]:
        return self.admin


class OpenBaoVault:
    """KV v2 at `secret/`: devices/{serial}, locks/{serial}, dashboard/admin. Passwords from password policies."""

    def __init__(self, addr: str, token: str, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=addr.rstrip("/") + "/v1", timeout=10)
        self.http.headers["X-Vault-Token"] = token

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise VaultUnavailable(f"{method} {path}: {type(error).__name__}") from None
        if response.status_code >= 500:
            raise VaultUnavailable(f"{method} {path}: HTTP {response.status_code}")
        return response

    def _read(self, path: str) -> tuple[int, dict] | None:
        response = self._call("GET", f"secret/data/{path}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()["data"]
        if body["data"] is None:
            return None
        return body["metadata"]["version"], body["data"]

    def _write(self, path: str, data: dict, cas: int) -> int:
        response = self._call("POST", f"secret/data/{path}", json={"options": {"cas": cas}, "data": data})
        if response.status_code == 400 and "check-and-set" in response.text:
            raise VaultConflict(f"{path}: version changed")
        response.raise_for_status()
        return response.json()["data"]["version"]

    def get_device(self, serial: str) -> DeviceRecord:
        found = self._read(f"devices/{serial}")
        if not found:
            raise KeyError(serial)
        version, data = found
        return DeviceRecord(**data, version=version)

    def put_device(self, record: DeviceRecord) -> None:
        record.version = self._write(f"devices/{record.serial}", record.data(), cas=record.version)

    def list_devices(self) -> list[str]:
        response = self._call("LIST", "secret/metadata/devices")
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return sorted(response.json()["data"]["keys"])

    def acquire_lock(self, serial: str, run_id: str) -> bool:
        try:
            # cas=0 means "create only if absent", which makes the lock atomic in the vault itself.
            self._write(f"locks/{serial}", {"run_id": run_id}, cas=0)
            return True
        except VaultConflict:
            return self.lock_holder(serial) == run_id

    def lock_holder(self, serial: str) -> str | None:
        found = self._read(f"locks/{serial}")
        return found[1]["run_id"] if found else None

    def release_lock(self, serial: str, run_id: str) -> None:
        if self.lock_holder(serial) == run_id:
            # Deleting metadata (not just the latest version) lets the next cas=0 create succeed.
            self._call("DELETE", f"secret/metadata/locks/{serial}").raise_for_status()

    def generate_password(self, model: str) -> str:
        response = self._call("GET", f"sys/policies/password/{MODEL_POLICY[model]}/generate")
        response.raise_for_status()
        return response.json()["data"]["password"]

    def admin_credential(self) -> tuple[str, str]:
        found = self._read("dashboard/admin")
        if not found:
            raise VaultError("dashboard admin credential missing; run scripts/seed.py")
        return found[1]["username"], found[1]["password"]

    def put_admin_credential(self, username: str, password: str) -> None:
        response = self._call("POST", "secret/data/dashboard/admin", json={"data": {"username": username, "password": password}})
        response.raise_for_status()
