"""Node implementations for the LangGraph workflow.

Each function is small, testable, and returns a partial state update.
Input state is never mutated in place.
"""

from __future__ import annotations

from .state import AgentState, ApprovalDecision, Route, make_event


def intake_node(state: AgentState) -> dict:
    """Normalize raw query: strip whitespace and log entry event."""
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using keyword heuristics.

    Priority: risky > error > tool > missing_info > simple.
    Uses word-boundary matching via regex to avoid substring false positives.
    """
    import re

    query = state.get("query", "").lower()
    words = set(re.findall(r"\b\w+\b", query))
    risk_level = "low"

    risky_kw = {"refund", "delete", "send", "cancel", "remove", "revoke"}
    error_kw = {"timeout", "fail", "failure", "error", "crash", "unavailable"}
    tool_kw = {"status", "order", "lookup", "check", "track", "find", "search"}
    pronoun_kw = {"it", "this", "that", "thing"}

    if words & risky_kw:
        route = Route.RISKY
        risk_level = "high"
    elif words & error_kw:
        route = Route.ERROR
    elif words & tool_kw:
        route = Route.TOOL
    elif len(words) < 5 and words & pronoun_kw:
        route = Route.MISSING_INFO
    else:
        route = Route.SIMPLE

    return {
        "route": route.value,
        "risk_level": risk_level,
        "events": [make_event("classify", "completed", f"route={route.value}")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information rather than hallucinating an answer."""
    query = state.get("query", "")
    question = f"Could you clarify what you mean by '{query}'? Please provide more context."
    return {
        "pending_question": question,
        "final_answer": question,
        "events": [make_event("clarify", "completed", "clarification requested")],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call with idempotent error simulation.

    Simulates transient failures for error-route scenarios so the retry loop
    can be exercised: first two attempts return ERROR, subsequent attempts succeed.
    """
    attempt = int(state.get("attempt", 0))
    sid = state.get("scenario_id", "unknown")
    if state.get("route") == Route.ERROR.value and attempt < 2:
        result = f"ERROR: transient failure attempt={attempt} scenario={sid}"
    else:
        result = f"mock-tool-result for scenario={sid}"
    return {
        "tool_results": [result],
        "events": [make_event("tool", "completed", f"tool executed attempt={attempt}")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for human approval with evidence and risk justification."""
    query = state.get("query", "")
    action = (
        f"Proposed: '{query}' — destructive action, "
        "requires human approval (risk_level=high)"
    )
    return {
        "proposed_action": action,
        "risk_level": "high",
        "events": [make_event("risky_action", "pending_approval", "awaiting human approval")],
    }


def approval_node(state: AgentState) -> dict:
    """Human approval step with optional LangGraph interrupt().

    Set LANGGRAPH_INTERRUPT=true for real HITL via interrupt().
    Default: mock approval so tests and CI run offline without human input.
    Rejected actions route to clarify instead of tool, preventing execution.
    """
    import os

    if os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true":
        from langgraph.types import interrupt

        value = interrupt({
            "proposed_action": state.get("proposed_action"),
            "risk_level": state.get("risk_level"),
        })
        if isinstance(value, dict):
            decision = ApprovalDecision(**value)
        else:
            decision = ApprovalDecision(approved=bool(value))
    else:
        decision = ApprovalDecision(approved=True, comment="mock approval for lab")
    return {
        "approval": decision.model_dump(),
        "events": [make_event("approval", "completed", f"approved={decision.approved}")],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt with bounded counter.

    Increments attempt counter; route_after_retry checks attempt >= max_attempts
    to bound the loop and redirect to dead_letter when budget is exhausted.
    """
    attempt = int(state.get("attempt", 0)) + 1
    errors = [f"transient failure attempt={attempt}"]
    return {
        "attempt": attempt,
        "errors": errors,
        "events": [make_event("retry", "completed", "retry recorded", attempt=attempt)],
    }


def answer_node(state: AgentState) -> dict:
    """Produce a final response grounded in tool_results or approval context."""
    tool_results = state.get("tool_results", [])
    approval = state.get("approval") or {}
    if tool_results:
        answer = f"Based on lookup: {tool_results[-1]}"
    elif approval.get("approved") and state.get("proposed_action"):
        answer = f"Action approved and executed: {state['proposed_action']}"
    else:
        answer = f"Answer to your question: {state.get('query', '')}"
    return {
        "final_answer": answer,
        "events": [make_event("answer", "completed", "answer generated")],
    }


def evaluate_node(state: AgentState) -> dict:
    """Check tool results — the 'done?' gate that enables bounded retry loops.

    Heuristic: any result containing 'ERROR' triggers a retry.
    Production upgrade: replace with an LLM-as-judge call for semantic validation.
    """
    tool_results = state.get("tool_results", [])
    latest = tool_results[-1] if tool_results else ""
    if "ERROR" in latest:
        return {
            "evaluation_result": "needs_retry",
            "events": [make_event("evaluate", "completed", "tool failed, retry needed")],
        }
    return {
        "evaluation_result": "success",
        "events": [make_event("evaluate", "completed", "tool result satisfactory")],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Log unresolvable failures for manual review.

    Third layer of error strategy: retry -> fallback -> dead letter.
    Persists error context for on-call investigation or support ticket creation.
    """
    attempt = state.get("attempt", 0)
    recent_errors = (state.get("errors") or [])[-3:]
    msg = (
        f"Request could not be completed after {attempt} attempt(s). "
        f"Recent errors: {recent_errors}. Logged for manual review."
    )
    return {
        "final_answer": msg,
        "events": [make_event("dead_letter", "completed", f"exhausted {attempt} attempts")],
    }


def finalize_node(state: AgentState) -> dict:
    """Finalize the run and emit a final audit event."""
    return {"events": [make_event("finalize", "completed", "workflow finished")]}
