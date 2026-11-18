from pathlib import Path

import streamlit as st

from constraintos.engine import run
from constraintos.state import encode, load, plan_document, totals

ROOT = Path(".constraintos")
st.set_page_config(page_title="ConstraintOS", layout="wide")
st.title("ConstraintOS")

uploaded = st.file_uploader("Event messages", type=["txt"])
if st.button("Process messages", disabled=uploaded is None, type="primary"):
    try:
        with st.spinner("Reading and validating messages"):
            status, _ = run(uploaded.getvalue().decode("utf-8"), ROOT)
        (st.success if status in {"complete", "duplicate"} else st.warning)(
            status.replace("_", " ").capitalize()
        )
    except (OSError, RuntimeError, ValueError) as exc:
        st.error(str(exc))

state = load(ROOT)
plan, trace, rejected = state["plan"], state["trace"], state["rejected"]
plan_tab, trace_tab, rejected_tab = st.tabs(["Plan", "Trace", f"Rejected ({len(rejected)})"])
with plan_tab:
    if not plan:
        st.info("Upload an event message pack to build a plan.")
    else:
        summary = st.columns(4)
        summary[0].metric("Venue", plan["venue"].get("name", plan["venue"]["selected"]))
        summary[1].metric("Event duration", f"{plan['schedule']['event_minutes']} min")
        summary[2].metric("Meals", plan["food"]["quantity"])
        summary[3].metric("Judges", plan["judging"]["judges"])
        for area in ("venue", "schedule", "food", "judging", "budget"):
            with st.expander(area.capitalize(), expanded=area == "budget"):
                st.json(plan[area])
        st.download_button("Download plan", encode(plan_document(state)), "plan.json", "application/json")
with trace_tab:
    token_totals = totals(trace)
    st.metric("Reported tokens", token_totals["total_tokens"])
    if token_totals["calls_without_reported_usage"]:
        st.caption(f"{token_totals['calls_without_reported_usage']} calls lacked provider token counts.")
    st.download_button("Download trace", encode(trace + [token_totals]), "trace.json", "application/json")
    for index, item in reversed(list(enumerate(trace, 1))):
        label = item["type"].replace("_", " ").capitalize()
        if item["type"] == "model_call":
            label += f" {item['number']} · {item['area']} · {item['model']}"
        elif "status" in item:
            label += f" · {item['status']}"
        with st.expander(f"{index}. {label}"):
            st.json(item)
with rejected_tab:
    if not rejected:
        st.info("No rejected messages.")
    for item in reversed(rejected):
        st.subheader(item["source"] or "Unknown source")
        st.write(item["reason"])
        st.code(item["message"], language=None)
