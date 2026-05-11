"""Streamlit HITL approval UI for LangGraph Agent.

Run with:
    streamlit run streamlit_app.py

Demonstrates Human-in-the-Loop: when a risky action is detected, the graph
pauses via interrupt() and waits for the human operator to Approve or Reject.
"""

from __future__ import annotations

import os
import sqlite3
import uuid

# Must be set before any langgraph import so approval_node uses real interrupt()
os.environ["LANGGRAPH_INTERRUPT"] = "true"

import streamlit as st  # noqa: E402

from langgraph.types import Command  # noqa: E402

from langgraph_agent_lab.graph import build_graph  # noqa: E402
from langgraph_agent_lab.state import Route, Scenario, initial_state  # noqa: E402

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Support Agent — HITL Demo",
    page_icon="🤖",
    layout="wide",
)
st.title("🤖 Support Ticket Agent — Human-in-the-Loop")
st.caption("Powered by LangGraph + SQLite persistence | Set LANGGRAPH_INTERRUPT=true")

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------
DEFAULTS: dict = {
    "query_input": "",
    "thread_id": None,
    "step": "idle",          # idle | waiting_approval | completed
    "interrupt_data": {},    # proposed_action, risk_level from interrupt()
    "final_state": {},
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

DB_PATH = "outputs/streamlit_checkpoints.db"

# ---------------------------------------------------------------------------
# Cached graph (keeps same SQLite connection across reruns)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_graph():
    from langgraph.checkpoint.sqlite import SqliteSaver
    import pathlib
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return build_graph(SqliteSaver(conn))


graph = get_graph()


# ---------------------------------------------------------------------------
# Helper: detect interrupt after invoke
# ---------------------------------------------------------------------------
def _get_interrupt_data(config: dict) -> dict | None:
    """Return interrupt payload if graph is paused, else None."""
    current = graph.get_state(config)
    if not current.next:
        return None
    for task in current.tasks:
        interrupts = getattr(task, "interrupts", [])
        if interrupts:
            return interrupts[0].value
    return {}


def _resume(config: dict, approved: bool, comment: str) -> dict:
    decision = {
        "approved": approved,
        "reviewer": "human-operator",
        "comment": comment or ("approved" if approved else "rejected"),
    }
    return graph.invoke(Command(resume=decision), config=config)


# ---------------------------------------------------------------------------
# Layout: two columns
# ---------------------------------------------------------------------------
left, right = st.columns([1, 1], gap="large")

# --- LEFT: query input & examples ---
with left:
    st.subheader("📝 Submit Request")

    EXAMPLES = {
        "💰 Risky: Refund": "Refund this customer and send confirmation email",
        "🗑️ Risky: Delete": "Delete customer account after support verification",
        "🔍 Tool: Lookup": "Please lookup order status for order 12345",
        "❓ Vague query": "Can you fix it?",
        "⚠️ Error / retry": "Timeout failure while processing request",
        "📖 Simple FAQ": "How do I reset my password?",
    }

    st.caption("Quick examples — click to load:")
    cols = st.columns(2)
    for idx, (label, q) in enumerate(EXAMPLES.items()):
        if cols[idx % 2].button(label, use_container_width=True):
            st.session_state.query_input = q
            st.session_state.step = "idle"

    query = st.text_area(
        "Or type your own request:",
        key="query_input",
        height=100,
        placeholder="e.g. Refund this customer and send confirmation email",
    )

    submitted = st.button(
        "🚀 Submit",
        type="primary",
        use_container_width=True,
        disabled=not query.strip(),
    )

    if submitted and query.strip():
        thread_id = f"hitl-{uuid.uuid4().hex[:8]}"
        st.session_state.thread_id = thread_id
        st.session_state.step = "running"
        st.session_state.interrupt_data = {}
        st.session_state.final_state = {}

        scenario = Scenario(
            id=thread_id,
            query=query.strip(),
            expected_route=Route.SIMPLE,   # classify_node overrides this
            max_attempts=3,
        )
        state = initial_state(scenario)
        state["thread_id"] = thread_id
        config = {"configurable": {"thread_id": thread_id}}

        with st.spinner("Agent is thinking..."):
            result = graph.invoke(state, config=config)

        interrupt_data = _get_interrupt_data(config)
        if interrupt_data is not None:
            st.session_state.step = "waiting_approval"
            st.session_state.interrupt_data = interrupt_data
        else:
            st.session_state.step = "completed"
            st.session_state.final_state = result

        st.rerun()

# --- RIGHT: status / approval / result panel ---
with right:
    step = st.session_state.step

    if step == "idle":
        st.info("Choose an example or type a request, then click **Submit**.")

        st.markdown("""
        **Supported routes:**
        | Route | Trigger |
        |---|---|
        | `simple` | FAQ / general questions |
        | `tool` | order, lookup, status |
        | `missing_info` | vague / short queries |
        | `risky` ⚠️ | refund, delete, send → **HITL pause** |
        | `error` | timeout, failure → retry loop |
        """)

    elif step == "running":
        st.spinner("Running...")

    elif step == "waiting_approval":
        st.subheader("⚠️ Approval Required")
        st.warning("The agent detected a **high-risk action** and has **paused** for your review.")

        data = st.session_state.interrupt_data
        proposed = data.get("proposed_action", "(no action text)")
        risk = data.get("risk_level", "high")

        st.markdown("**Proposed action:**")
        st.code(proposed, language=None)
        st.markdown(f"**Risk level:** `{risk.upper()}`")

        comment = st.text_input(
            "Comment (optional):",
            placeholder="Reason for your decision...",
            key="approval_comment",
        )

        col_a, col_b = st.columns(2)
        config = {"configurable": {"thread_id": st.session_state.thread_id}}

        with col_a:
            if st.button("✅ Approve", type="primary", use_container_width=True):
                with st.spinner("Resuming agent after approval..."):
                    result = _resume(config, approved=True, comment=comment)
                st.session_state.step = "completed"
                st.session_state.final_state = result
                st.rerun()

        with col_b:
            if st.button("❌ Reject", use_container_width=True):
                with st.spinner("Resuming agent after rejection..."):
                    result = _resume(config, approved=False, comment=comment)
                st.session_state.step = "completed"
                st.session_state.final_state = result
                st.rerun()

    elif step == "completed":
        final = st.session_state.final_state
        route = final.get("route", "unknown")
        attempt = final.get("attempt", 0)
        approval = final.get("approval") or {}

        # Status badge
        if route == "risky" and approval:
            if approval.get("approved"):
                st.success("✅ Action approved and executed!")
            else:
                st.warning("❌ Action rejected — clarification requested.")
        elif route == "error" and attempt > 0:
            st.info(f"🔁 Completed after {attempt} retry attempt(s).")
        elif route == "missing_info":
            st.info("❓ Query was too vague — clarification sent.")
        else:
            st.success("✅ Request completed!")

        # Response
        answer = final.get("final_answer") or final.get("pending_question", "")
        if answer:
            st.markdown("**Agent response:**")
            st.markdown(f"> {answer}")

        # Metadata
        with st.expander("Details", expanded=False):
            st.json({
                "route": route,
                "attempt": attempt,
                "approval": approval or None,
                "thread_id": st.session_state.thread_id,
            })

        if st.button("🔄 New query", use_container_width=True):
            for k, v in DEFAULTS.items():
                st.session_state[k] = v
            st.rerun()

# ---------------------------------------------------------------------------
# Audit trail (shown after any action)
# ---------------------------------------------------------------------------
if st.session_state.step in ("waiting_approval", "completed") and st.session_state.thread_id:
    st.divider()
    st.subheader("📋 Audit Trail")

    config = {"configurable": {"thread_id": st.session_state.thread_id}}
    current = graph.get_state(config)
    events = current.values.get("events", [])

    if events:
        for ev in events:
            node = ev.get("node", "?")
            etype = ev.get("event_type", "?")
            msg = ev.get("message", "")
            icon = {
                "intake": "📥", "classify": "🔀", "tool": "🔧",
                "evaluate": "🔍", "answer": "💬", "clarify": "❓",
                "risky_action": "⚠️", "approval": "👤", "retry": "🔁",
                "dead_letter": "💀", "finalize": "✅",
            }.get(node, "•")
            st.markdown(f"{icon} `{node}` → **{etype}**: {msg}")
    else:
        st.caption("No events recorded yet.")
