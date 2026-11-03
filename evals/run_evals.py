"""Layer 2: how well does the real agent do the job?

Each scenario runs the full protocol against a fresh in-process dashboard, several times, because LLM runs vary.
With LANGSMITH_API_KEY set it runs as a LangSmith experiment, so models can be compared side by side.

    python evals/run_evals.py --model gpt-6-luna --reps 5
    python evals/run_evals.py --changer scripted --reps 1     # checks the harness itself, no LLM cost
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "evals")]

from dotenv import load_dotenv  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from rotator.browser import BrowserWorker  # noqa: E402
from rotator.changers import LLMChanger, ScriptedChanger  # noqa: E402
from rotator.config import Settings  # noqa: E402
from rotator.device import DeviceProbe  # noqa: E402
from rotator.protocol import Deps, build_graph, start_run  # noqa: E402
from scenarios import BY_NAME, SCENARIOS  # noqa: E402
from testbed import start_dashboard  # noqa: E402

MUST_BE_ZERO = ("lost_password", "wrong_device_changes", "secret_leaks")


class Harness:
    def __init__(self, changer, workdir: Path):
        self.changer = changer
        self.workdir = workdir
        self.dashboard = start_dashboard(workdir / "dashboard.db")
        self.worker = BrowserWorker()
        self.all_secrets: set[str] = set()

    def run(self, scenario_name: str) -> dict:
        scenario = BY_NAME[scenario_name]
        dash = self.dashboard
        dash.reset()
        dash.faults(**scenario.faults)
        run_id = uuid.uuid4().hex[:12]
        data_dir = self.workdir / "runs" / run_id
        deps = Deps(vault=dash.vault, probe=DeviceProbe(dash.url), changer=self.changer, worker=self.worker,
                    settings=Settings(dashboard_url=dash.url, data_dir=data_dir), clock=dash.clock)
        checkpoints = data_dir / "checkpoints.db"
        data_dir.mkdir(parents=True)
        graph = build_graph(deps, SqliteSaver(sqlite3.connect(checkpoints, check_same_thread=False)))
        started = time.monotonic()
        view = start_run(graph, scenario.serial, run_id=run_id)
        seconds = time.monotonic() - started

        record = dash.vault.get_device(scenario.serial)
        secrets = set(dash.passwords.values()) | {record.active, record.pending, record.previous} - {""}
        self.all_secrets |= secrets
        others_changed = [s for s, p in dash.passwords.items() if s != scenario.serial and dash.truth(s) != p]
        files = [checkpoints, *(data_dir / "runs").rglob("*")]
        blob = b"".join(f.read_bytes() for f in files if f.is_file())
        events = deps.audit(run_id).read()
        changes = [e for e in events if e["step"] in ("change", "change_failed")]
        outcome = view.get("outcome") or view.get("status")
        return {
            "scenario": scenario.name,
            "outcome": outcome,
            "reason": view.get("reason", ""),
            "correct": int(outcome == scenario.expect),
            "lost_password": int(dash.truth(scenario.serial) not in {record.active, record.pending}),
            "wrong_device_changes": len(others_changed),
            "secret_leaks": sum(1 for s in secrets if s.encode() in blob),
            "change_attempts": view.get("change_attempts", 0),
            "gate_rejections": sum(1 for e in events if e["step"] == "gate_rejected"),
            "tool_calls": sum(e.get("tool_calls", 0) for e in changes),
            "input_tokens": sum(e.get("input_tokens", 0) for e in changes),
            "output_tokens": sum(e.get("output_tokens", 0) for e in changes),
            "seconds": round(seconds, 1),
            "reports": [e.get("report") for e in changes],
        }

    def close(self) -> None:
        self.worker.close()
        self.dashboard.stop()


def metric_evaluator(outputs: dict) -> dict:
    keys = ("correct", *MUST_BE_ZERO, "gate_rejections", "change_attempts", "tool_calls", "input_tokens",
            "output_tokens", "seconds")
    return {"results": [{"key": k, "score": outputs[k]} for k in keys]}


def run_local(harness: Harness, scenarios: list[str], reps: int) -> list[dict]:
    rows = []
    for name in scenarios:
        for rep in range(reps):
            row = harness.run(name) | {"rep": rep}
            print(f"{name:22} rep {rep}  {row['outcome']:10} correct={row['correct']} "
                  f"tools={row['tool_calls']} tokens={row['input_tokens'] + row['output_tokens']} {row['seconds']}s")
            rows.append(row)
    return rows


def run_langsmith(harness: Harness, scenarios: list[str], reps: int, model: str) -> tuple[list[dict], str]:
    from langsmith import Client, evaluate

    client = Client()
    dataset = "legacy-password-rotation-scenarios"
    if not client.has_dataset(dataset_name=dataset):
        client.create_dataset(dataset, description="Rotation scenarios for the legacy dashboard agent.")
        client.create_examples(dataset_name=dataset, examples=[
            {"inputs": {"scenario": s.name}, "outputs": {"expect": s.expect}, "metadata": {"about": s.about}}
            for s in SCENARIOS])
    examples = [e for e in client.list_examples(dataset_name=dataset) if e.inputs["scenario"] in scenarios]

    def target(inputs: dict) -> dict:
        row = harness.run(inputs["scenario"])
        print(f"{row['scenario']:22} {row['outcome']:10} correct={row['correct']} tools={row['tool_calls']}")
        return row

    results = evaluate(target, data=examples, evaluators=[metric_evaluator], num_repetitions=reps,
                       max_concurrency=1, experiment_prefix=model, metadata={"model": model})
    rows = [r["run"].outputs for r in results if r["run"].outputs]
    return rows, results.experiment_name


def scan_langsmith(experiment: str, secrets: set[str]) -> int:
    """Proves no secret reached the tracing provider: searches every traced run's inputs and outputs."""
    from langsmith import Client

    found = 0
    for run in Client().list_runs(project_name=experiment):
        blob = json.dumps([run.inputs, run.outputs, run.error], default=str)
        found += sum(1 for s in secrets if s in blob)
    return found


def summarise(rows: list[dict], model: str, traces_checked: str) -> str:
    lines = [f"# Eval results: {model}", "", f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
             f"Trace leak scan: {traces_checked}.", "",
             "| Scenario | Runs | Correct | Lost pw | Wrong device | Leaks | Gate rejections | Avg tool calls | Avg tokens | Avg s |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name in dict.fromkeys(r["scenario"] for r in rows):
        rs = [r for r in rows if r["scenario"] == name]
        lines.append(
            f"| {name} | {len(rs)} | {sum(r['correct'] for r in rs)}/{len(rs)} | {sum(r['lost_password'] for r in rs)} | "
            f"{sum(r['wrong_device_changes'] for r in rs)} | {sum(r['secret_leaks'] for r in rs)} | "
            f"{sum(r['gate_rejections'] for r in rs)} | {mean(r['tool_calls'] for r in rs):.1f} | "
            f"{mean(r['input_tokens'] + r['output_tokens'] for r in rs):,.0f} | {mean(r['seconds'] for r in rs):.1f} |")
    total = len(rows)
    lines += ["", f"**Overall:** {sum(r['correct'] for r in rows)}/{total} correct. "
              + ", ".join(f"{k} = {sum(r[k] for r in rows)}" for k in MUST_BE_ZERO) + " (must all be 0)."]
    return "\n".join(lines) + "\n"


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("ROTATOR_MODEL", "gpt-6-luna"))
    parser.add_argument("--effort", default=os.environ.get("ROTATOR_REASONING_EFFORT") or None,
                        help="reasoning effort, e.g. low or medium; unset uses the model's default")
    parser.add_argument("--changer", choices=["llm", "scripted"], default="llm")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--scenarios", nargs="*", default=[s.name for s in SCENARIOS])
    parser.add_argument("--no-langsmith", action="store_true")
    args = parser.parse_args()

    changer = ScriptedChanger() if args.changer == "scripted" else LLMChanger(args.model, reasoning_effort=args.effort)
    label = "scripted" if args.changer == "scripted" else args.model + (f"-{args.effort}" if args.effort else "")
    use_langsmith = bool(os.environ.get("LANGSMITH_API_KEY")) and not args.no_langsmith
    with tempfile.TemporaryDirectory() as tmp:
        harness = Harness(changer, Path(tmp))
        try:
            if use_langsmith:
                rows, experiment = run_langsmith(harness, args.scenarios, args.reps, label)
                time.sleep(10)  # traces upload in the background
                leaks = scan_langsmith(experiment, harness.all_secrets)
                traces = f"{leaks} secrets found in LangSmith experiment `{experiment}`"
            else:
                rows = run_local(harness, args.scenarios, args.reps)
                traces = "not run (LangSmith disabled)"
        finally:
            harness.close()

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    (out_dir / f"{stamp}-{label}.json").write_text(json.dumps(rows, indent=2))
    summary = summarise(rows, label, traces)
    (out_dir / f"{stamp}-{label}.md").write_text(summary)
    print("\n" + summary)


if __name__ == "__main__":
    main()
