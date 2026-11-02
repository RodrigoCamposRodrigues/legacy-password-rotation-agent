import httpx


class DeviceUnreachable(Exception):
    """The device did not answer. This says nothing about whether a password is right."""


class DeviceProbe:
    """Logs in to a device directly, outside the dashboard, to check which password it accepts."""

    def __init__(self, base_url: str, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=base_url, timeout=10)

    def check(self, serial: str, password: str) -> bool:
        try:
            response = self.http.post(f"/device/{serial}/login", json={"password": password})
        except httpx.HTTPError as error:
            raise DeviceUnreachable(f"{serial}: {type(error).__name__}") from None
        if response.status_code == 200:
            return True
        if response.status_code == 401:
            return False
        raise DeviceUnreachable(f"{serial}: HTTP {response.status_code}")
