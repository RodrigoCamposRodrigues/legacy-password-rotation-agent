from typing import Literal

from pydantic import BaseModel

Mode = Literal["off", "once", "always"]


class Faults(BaseModel):
    slow_ms: int = 0
    session_expiry_requests: int = 0
    save_error: Mode = "off"
    silent_failure: Mode = "off"

    def take(self, name: str) -> bool:
        """True if the fault fires now. A `once` fault turns itself off after firing."""
        mode = getattr(self, name)
        if mode == "once":
            setattr(self, name, "off")
        return mode != "off"
