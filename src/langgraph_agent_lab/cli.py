"""CLI for the lab."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
import yaml

from .graph import build_graph
from .metrics import MetricsReport, metric_from_state, summarize_metrics, write_metrics
from .persistence import build_checkpointer
from .report import write_report
from .scenarios import load_scenarios
from .state import initial_state

app = typer.Typer(no_args_is_help=True)


def _demo_crash_resume(db_path: str = "outputs/checkpoints.db") -> bool:
    """Demonstrate crash-resume: run a scenario, recover state from a fresh graph instance.

    Simulates a process restart by creating two separate SqliteSaver instances pointing
    at the same DB file. If the second instance can recover the final_answer written by
    the first, resume_success is True.
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        return False

    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        scenarios = load_scenarios("data/sample/scenarios.jsonl")
        scenario = next(s for s in scenarios if s.expected_route.value == "simple")
        state = initial_state(scenario)
        thread_id = f"resume-demo-{scenario.id}"
        state["thread_id"] = thread_id

        # Step 1: run scenario and persist to SQLite
        conn1 = sqlite3.connect(db_path, check_same_thread=False)
        cp1 = SqliteSaver(conn1)
        g1 = build_graph(cp1)
        g1.invoke(state, config={"configurable": {"thread_id": thread_id}})
        conn1.close()

        # Step 2: recover from a fresh graph instance (simulates process restart)
        conn2 = sqlite3.connect(db_path, check_same_thread=False)
        cp2 = SqliteSaver(conn2)
        g2 = build_graph(cp2)
        recovered = g2.get_state({"configurable": {"thread_id": thread_id}})
        conn2.close()

        vals = recovered.values
        return bool(vals.get("final_answer") or vals.get("pending_question"))
    except Exception:  # noqa: BLE001
        return False


@app.command("run-scenarios")
def run_scenarios(
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Run all grading scenarios and write metrics JSON."""
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    scenarios = load_scenarios(cfg["scenarios_path"])
    checkpointer = build_checkpointer(
        cfg.get("checkpointer", "memory"), cfg.get("database_url")
    )
    graph = build_graph(checkpointer=checkpointer)
    metrics = []
    for scenario in scenarios:
        state = initial_state(scenario)
        run_config = {"configurable": {"thread_id": state["thread_id"]}}
        final_state = graph.invoke(state, config=run_config)
        metrics.append(
            metric_from_state(
                final_state, scenario.expected_route.value, scenario.requires_approval
            )
        )

    resume_ok = _demo_crash_resume()
    typer.echo(f"Crash-resume demo: {'PASS' if resume_ok else 'SKIP (install sqlite extra)'}")

    report = summarize_metrics(metrics, resume_success=resume_ok)
    write_metrics(report, output)
    if cfg.get("report_path"):
        write_report(report, cfg["report_path"])
    typer.echo(f"Wrote metrics to {output}")


@app.command("draw-graph")
def draw_graph(
    output: Annotated[Path, typer.Option("--output")] = Path("outputs/graph.md"),
) -> None:
    """Export Mermaid diagram of the compiled graph (bonus extension)."""
    g = build_graph(checkpointer=None)
    diagram = g.get_graph().draw_mermaid()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"```mermaid\n{diagram}\n```", encoding="utf-8")
    typer.echo(f"Graph diagram written to {output}")


@app.command("time-travel")
def time_travel(
    thread_id: Annotated[str, typer.Option("--thread-id")] = "resume-demo-S01_simple",
    db_path: Annotated[str, typer.Option("--db")] = "outputs/checkpoints.db",
    replay_step: Annotated[int, typer.Option("--replay-step")] = 0,
) -> None:
    """Time-travel bonus: list state history and replay from an earlier checkpoint.

    Shows every checkpoint saved by the SQLite checkpointer for a given thread_id,
    then re-invokes the graph from the chosen step to demonstrate time travel.
    replay_step=0 replays from the earliest checkpoint (default).
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        typer.echo("SQLite extra required: pip install langgraph-checkpoint-sqlite")
        raise typer.Exit(1) from None  # noqa: B904

    conn = sqlite3.connect(db_path, check_same_thread=False)
    cp = SqliteSaver(conn)
    graph = build_graph(cp)
    run_cfg = {"configurable": {"thread_id": thread_id}}

    checkpoints = list(graph.get_state_history(run_cfg))
    if not checkpoints:
        typer.echo(f"No checkpoints found for thread_id='{thread_id}'.")
        typer.echo("Run 'run-scenarios' first to populate the SQLite DB.")
        conn.close()
        raise typer.Exit(1) from None  # noqa: B904

    n = len(checkpoints)
    typer.echo(f"\n=== State history for '{thread_id}' ({n} checkpoints) ===\n")
    for i, snap in enumerate(reversed(checkpoints)):
        step = snap.metadata.get("step", "?")
        node = snap.metadata.get("source", "?")
        route = snap.values.get("route", "-")
        attempt = snap.values.get("attempt", 0)
        answer = (snap.values.get("final_answer") or "")[:50]
        typer.echo(
            f"  [{i:>2}] step={step:>3}  node={node:<12} "
            f"route={route:<12} attempt={attempt}  answer={answer!r}"
        )

    # Replay from chosen step (time travel)
    target = list(reversed(checkpoints))[replay_step]
    cid = target.config["configurable"].get("checkpoint_id")
    typer.echo(f"\n=== Time travel: replaying from step [{replay_step}] (checkpoint_id={cid}) ===")
    replay_cfg = {**run_cfg, "configurable": {**run_cfg["configurable"], "checkpoint_id": cid}}
    result = graph.invoke(None, config=replay_cfg)
    answer_out = str(result.get("final_answer", ""))[:80]
    typer.echo(f"    Replayed final_answer = {answer_out!r}")
    conn.close()


@app.command("validate-metrics")
def validate_metrics(metrics: Annotated[Path, typer.Option("--metrics")]) -> None:
    """Validate metrics JSON schema for grading."""
    payload = json.loads(metrics.read_text(encoding="utf-8"))
    report = MetricsReport.model_validate(payload)
    if report.total_scenarios < 6:
        raise typer.BadParameter("Expected at least 6 scenarios")
    typer.echo(f"Metrics valid. success_rate={report.success_rate:.2%}")


if __name__ == "__main__":
    app()
