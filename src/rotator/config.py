import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}


def _bao_token() -> str:
    if token := os.environ.get("BAO_TOKEN"):
        return token
    state = REPO_ROOT / ".bao" / "state.json"
    if state.exists():
        return json.loads(state.read_text())["rotator_token"]
    return ""


@dataclass(frozen=True)
class Defences:
    """Switches for security ablation (agent-security-harness). Off means weaker; production keeps all on."""

    placeholders: bool = True
    submit_gate: bool = True
    untrusted_marking: bool = True
    budgets: bool = True
    verify: bool = True

    def with_disabled(self, names: Iterable[str]) -> "Defences":
        names = set(names)
        if unknown := names - set(DEFENCE_NAMES):
            raise ValueError(f"unknown defence(s): {', '.join(sorted(unknown))}")
        return replace(self, **{name: False for name in names})


DEFENCE_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Defences))


@dataclass
class Settings:
    dashboard_url: str = "http://127.0.0.1:8000"
    bao_addr: str = "http://127.0.0.1:8200"
    bao_token: str = ""
    model: str = "gpt-6-luna"
    # None leaves the provider's default effort for the model.
    reasoning_effort: str | None = None
    data_dir: Path = REPO_ROOT / "data"
    supervised: bool = False
    clock_offset_days: float = 0
    max_tool_calls: int = 40
    max_tokens: int = 60_000
    max_seconds: float = 180
    verify_retries: int = 2
    rotation_days: int = 180
    # A lock whose run this service doesn't know is only taken over after this long (longest run is ~10 min).
    lock_ttl_seconds: float = 3600
    defences: Defences = field(default_factory=Defences)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(REPO_ROOT / ".env")
        return cls(
            dashboard_url=os.environ.get("DASHBOARD_URL", cls.dashboard_url),
            bao_addr=os.environ.get("BAO_ADDR", cls.bao_addr),
            bao_token=_bao_token(),
            model=os.environ.get("ROTATOR_MODEL", cls.model),
            reasoning_effort=os.environ.get("ROTATOR_REASONING_EFFORT") or None,
            data_dir=Path(os.environ.get("ROTATOR_DATA_DIR", REPO_ROOT / "data")),
            supervised=_flag("SUPERVISED"),
            clock_offset_days=float(os.environ.get("CLOCK_OFFSET_DAYS", "0")),
        )
