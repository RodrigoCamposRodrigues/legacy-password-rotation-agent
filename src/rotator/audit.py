import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Per-run JSON-lines log plus screenshots. Callers pass only non-secret fields."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "audit.jsonl"
        self._shots = len(list(self.run_dir.glob("*.png")))

    def event(self, step: str, **fields) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "step": step, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, default=str) + "\n")

    def screenshot(self, page, name: str) -> str:
        self._shots += 1
        filename = f"{self._shots:03d}-{name}.png"
        page.screenshot(path=self.run_dir / filename, mask=[page.locator("input[type=password]")])
        self.event("screenshot", file=filename, url=page.url)
        return filename

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
