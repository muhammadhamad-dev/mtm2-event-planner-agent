from decimal import Decimal
from pathlib import Path

import altair as alt
import streamlit as st

from constraintos.engine import run
from constraintos.state import encode, load, plan_document, totals

ROOT = Path(".constraintos")
AREAS = ("Venue", "Schedule", "Food", "Judging", "Budget")
RUN_LABELS = {
    "complete": "Processing complete", "duplicate": "Duplicate skipped",
    "provider_error": "Provider request failed", "extraction_error": "Source extraction failed",
    "incomplete": "Plan incomplete",
}

st.set_page_config(page_title="ConstraintOS", page_icon=":material/account_tree:", layout="wide")


def label(value):
    return str(value).replace("_", " ").replace("-", " ").capitalize()


def option_name(option):
    return option.get("name") or option["selected"].replace("_", " ").replace("-", " ").title()


def money(amount, currency):
    number = Decimal(str(amount))
    precision = 0 if number == number.to_integral() else 2
    return f"{currency} {number:,.{precision}f}".strip()


def clock(minutes, relative):
    hours, minutes = divmod(minutes, 60)
    return f"{'+' if relative else ''}{hours:02}:{minutes:02}"


def show_table(rows, description, **options):
    st.dataframe(rows, hide_index=True, alt=description, **options)


def evidence(state, area):
    facts = [item for key, item in state["facts"].items() if key.split(".")[0] == area]
    rules = [item for item in state["rules"].values() if item["key"].split(".")[0] == area]
    if not facts and not rules:
        return
    with st.expander("Source evidence and constraints"):
        show_table([
            {
                "Type": "Constraint" if item in rules else "Source fact",
                "Field": item["key"],
                "Source": item.get("source") or "Unknown",
                "Date": item.get("date") or "Unstated",
                "Evidence": item.get("evidence", ""),
            }
            for item in facts + rules
        ], f"Source evidence and constraints for {area}")


def schedule_view(schedule):
    st.subheader("Schedule")
    relative = schedule.get("relative_times", True)
    blocks = [
        {"Activity": row["activity"], "Start": clock(row["start"], relative),
         "End": clock(row["end"], relative), "Minutes": row["end"] - row["start"],
         "From": row["start"] - schedule["start"], "Until": row["end"] - schedule["start"]}
        for row in schedule["timeline"]
    ]
    if blocks:
        chart = alt.Chart(alt.Data(values=blocks)).mark_bar(color="#0D6C59", cornerRadius=2, size=14).encode(
            x=alt.X("From:Q", title="Minutes from start", axis=alt.Axis(tickCount=6)),
            x2="Until:Q",
            y=alt.Y("Activity:N", sort=None, title=None,
                    axis=alt.Axis(labelLimit=140, labelColor="#40544D")),
            tooltip=["Activity:N", "Start:N", "End:N", "Minutes:Q"],
        )
        st.altair_chart(chart, height=max(160, len(blocks) * 28 + 65),
                        alt="Timeline of event activities by elapsed minute")
        show_table(
            [{key: row[key] for key in ("Activity", "Start", "End", "Minutes")} for row in blocks],
            "Exact schedule activity times",
        )


def budget_view(budget):
    currency = budget.get("currency") or ""
    st.subheader("Budget ledger")
    with st.grid(3, min_column_width=145):
        for key, title in (("cap", "Spending cap"), ("total", "Committed spend"), ("remaining", "Remaining")):
            st.metric(title, money(budget[key], currency))
    rows = [
        {"Expense": label(key), "Amount": money(value, currency)}
        for key, value in budget["lines"].items() if key != "commitments"
    ]
    rows.extend({"Expense": label(key), "Amount": money(value, currency)}
                for key, value in budget.get("commitments", {}).items())
    show_table(rows, "Selected costs and named budget commitments")


def plan_view(state):
    plan = state["plan"]
    if not plan:
        st.info("Process a message pack to see the event plan and its supporting evidence.")
        return
    venue, schedule, food, judging, budget = (plan[area.lower()] for area in AREAS)
    currency = budget.get("currency") or ""
    with st.container(border=True):
        schedule_view(schedule)
        evidence(state, "schedule")
    with st.grid(2, min_column_width=290):
        with st.container(border=True):
            st.subheader("Venue")
            st.metric("Selected venue", option_name(venue))
            st.text(f"{venue['capacity']:,} seats for {venue['required_capacity']:,} people")
            st.text(f"{venue['capacity'] - venue['required_capacity']:,} spare seats")
            st.text(money(venue["cost"], currency))
            features = venue.get("features", [])
            if features:
                st.text(", ".join(map(label, features)) if isinstance(features, list) else str(features))
            evidence(state, "venue")
        with st.container(border=True):
            st.subheader("Food")
            st.metric("Selected meal", option_name(food))
            st.text(f"{food['quantity']:,} meals for {food['headcount']:,} people")
            st.text(f"{money(food['unit_cost'], currency)} per meal · {money(food['cost'], currency)} total")
            evidence(state, "food")
    with st.container(border=True):
        st.subheader("Judging")
        with st.grid(3, min_column_width=130):
            st.metric("Judges", judging["judges"])
            st.metric("Team coverage", f"{judging['teams']} / {judging['team_capacity']}")
            st.metric("Judging time", f"{judging['duration']} min")
        st.caption(f"{judging['waves']} rounds · {judging['minutes_per_team']} minutes per team")
        if judging.get("assignments"):
            with st.expander("Judge assignments"):
                show_table([{"Judge": row["judge"], "Teams": ", ".join(map(str, row["teams"]))}
                            for row in judging["assignments"]], "Team assignments by judge")
        evidence(state, "judging")
    with st.container(border=True):
        budget_view(budget)
        evidence(state, "budget")


def overview(state, token_totals, last_run):
    plan = state["plan"]
    if plan:
        venue, schedule, food, judging, budget = (plan[area.lower()] for area in AREAS)
        currency = budget.get("currency") or ""
        with st.grid(4, min_column_width=170):
            st.metric("Venue", option_name(venue), border=True)
            st.metric("Attendance / meals", f"{food.get('event_attendance') or venue['required_capacity']} / {food['quantity']}",
                      border=True)
            st.metric("Judges", judging["judges"], border=True)
            st.metric("Committed spend", money(budget["total"], currency), border=True)
        validation, budget_panel = st.columns([1.32, 1])
        with validation, st.container(border=True):
            st.subheader("Plan validation")
            checks = {
                "Venue": f"{venue['capacity']} seats · {venue['required_capacity']} required",
                "Schedule": f"{schedule['event_minutes']} min · {len(schedule['timeline'])} blocks",
                "Food": f"{food['quantity']} meals · {food['headcount']} required",
                "Judging": f"{judging['team_capacity']} slots · {judging['teams']} teams",
                "Budget": f"{money(budget['remaining'], currency)} available",
            }
            with st.grid(2, min_column_width=150):
                for area, detail in checks.items():
                    with st.container():
                        st.badge(area, color="green", icon=":material/check:")
                        st.caption(detail)
        with budget_panel, st.container(border=True):
            st.subheader("Budget utilization")
            cap = Decimal(str(budget["cap"]))
            if cap:
                share = Decimal(str(budget["total"])) / cap
                st.progress(float(max(0, min(1, share))), text=f"{share:.1%} of spending cap")
            else:
                st.caption("Spending cap is zero.")
            st.metric("Remaining", money(budget["remaining"], currency))
            st.caption(f"Cap {money(budget['cap'], currency)}")
    else:
        st.info("Load a TXT file to build and validate the first plan.")
    if last_run:
        with st.container(border=True):
            st.subheader("Execution")
            with st.grid(3, min_column_width=125):
                st.metric("Last run", f"{last_run['wall_seconds']:.3f} s")
                st.metric("Logged model calls", token_totals["model_calls"])
                st.metric("Reported tokens", f"{token_totals['total_tokens']:,}")


def trace_view(trace, token_totals):
    st.subheader("Execution trace")
    if not trace:
        st.info("Execution history appears after the first run.")
        return
    with st.grid(3, min_column_width=125):
        st.metric("Model calls", token_totals["model_calls"])
        st.metric("Reported input", f"{token_totals['prompt_tokens']:,}")
        st.metric("Reported output", f"{token_totals['completion_tokens']:,}")
    st.caption(f"{token_totals['total_tokens']:,} total reported tokens")
    if token_totals["calls_without_reported_usage"]:
        st.caption(f"Provider omitted usage on {token_totals['calls_without_reported_usage']} calls.")
    selection = st.segmented_control("Show trace events", ["All", "Model calls", "Deterministic"], default="All",
                                     key="trace_filter")
    for index, item in enumerate(trace, 1):
        is_call = item["type"] == "model_call"
        if (selection == "Model calls" and not is_call) or (selection == "Deterministic" and is_call):
            continue
        if is_call:
            usage = item.get("usage") or {}
            token_line = "Usage unavailable"
            if all(type(usage.get(field)) is int for field in ("prompt_tokens", "completion_tokens", "total_tokens")):
                token_line = f"{usage['prompt_tokens']:,} in · {usage['completion_tokens']:,} out · {usage['total_tokens']:,} total"
            status = f"HTTP {item['status']}" if "status" in item else item.get("error", "No HTTP status")
            title = f"Call {item['number']:02} · {item['area']} · {item['model']} · {status}"
            count = f"{usage['total_tokens']:,} tokens" if type(usage.get("total_tokens")) is int else "Usage unavailable"
            details = st.expander(f"{title} · {item['latency_seconds']:.3f} s · {count}",
                                  key=f"trace_call_{index}", on_change="rerun")
            if details.open:
                with details:
                    st.text(token_line)
                    st.caption(item.get("started_at", ""))
                    response = item.get("output")
                    if isinstance(response, dict) and isinstance(response.get("x_groq"), dict):
                        request_id = response["x_groq"].get("id")
                        if request_id:
                            st.text(f"Groq request {request_id}")
                    for key, heading in (("input", "Raw request"), ("output", "Raw response")):
                        st.subheader(heading)
                        st.code(encode(item.get(key)), language="json", wrap_lines=True)
        else:
            with st.expander(f"Event {index:02} · {label(item.get('status', item['type']))}"):
                show_event(item)
                if item.get("checks"):
                    st.text("Validated: " + ", ".join(item.get("validated_areas", [])))
                if item.get("reasons"):
                    st.text("\n".join(item["reasons"]))
                if item.get("wall_seconds") is not None:
                    st.text(f"{item['wall_seconds']:.3f} seconds")
                if item.get("wait_seconds") is not None:
                    st.text(f"Waited {item['wait_seconds']} seconds")


def show_event(item):
    if item.get("source"):
        st.text(item["source"])
    if item.get("message"):
        st.code(item["message"], language=None, wrap_lines=True)
    if item.get("reason"):
        st.text(item["reason"])
    if item.get("affected_areas"):
        st.caption("Affected areas · " + ", ".join(item["affected_areas"]))
    if item.get("decisions"):
        st.text("\n".join(item["decisions"]))


def changes_view(state):
    rejected = state["rejected"]
    st.subheader("Rejected messages")
    if not rejected:
        st.caption("No rejected messages recorded.")
    for item in reversed(rejected):
        with st.container(border=True):
            st.badge("Rejected", icon=":material/block:", color="red")
            st.text(item.get("source") or "Unknown source")
            st.text(item["reason"])
            st.code(item["message"], language=None, wrap_lines=True)
    if rejected and state["plan"]:
        st.caption("Rejected changes were excluded from the active plan.")
    st.subheader("Changes and advisories")
    events = [item for item in state["trace"] if item.get("status") == "advisory"
              or (item.get("status") == "accepted" and item.get("source"))
              or (item.get("status") == "analyzed" and (item.get("decisions") or item.get("changed_areas")))]
    if not events:
        st.caption("No changes or advisories recorded.")
    for item in reversed(events):
        heading = {"advisory": "Advisory", "accepted": "Accepted update"}.get(item["status"])
        with st.expander(heading or "Dependency analysis · " + ", ".join(item.get("changed_areas", []))):
            show_event(item)
            if item.get("validated_areas"):
                st.caption("Validated · " + ", ".join(item["validated_areas"]))
    proposals = state["proposals"]
    if proposals:
        with st.expander(f"Unapproved proposals ({len(proposals)})"):
            for item in proposals:
                st.text(item.get("source") or "Unknown source")
                st.code(item["evidence"], language=None, wrap_lines=True)


st.html("""
<style>
.cos-masthead {
    background: #12332e; color: #f9f7ef; padding: clamp(20px, 3vw, 34px);
    border: 1px solid #12332e; border-radius: 7px;
}
.cos-masthead h1 { color: inherit; font-size: clamp(2.15rem, 5vw, 4rem);
    letter-spacing: -.075em; line-height: 1.06; padding: 0; margin: 7px 0 12px; }
.cos-masthead p { color: #d6e8de; margin: 0; font-size: 1rem; }
.cos-masthead .cos-kicker { font-size: .72rem; font-weight: 650; letter-spacing: .16em; }
</style>
<div class="cos-masthead">
    <p class="cos-kicker">EVENT DECISION INTELLIGENCE</p>
    <h1>ConstraintOS</h1>
    <p>Event plan, source evidence, and execution history.</p>
</div>
""")

with st.container(border=True):
    st.subheader("Process a message file")
    upload, action = st.columns([3, 1], vertical_alignment="bottom")
    with upload:
        uploaded = st.file_uploader("Event messages", type=["txt"], key="event_messages",
                                    label_visibility="collapsed")
    with action:
        process = st.button("Process messages", disabled=uploaded is None, type="primary",
                            key="process_messages", width="stretch")
    st.caption("Use the same TXT upload for the initial pack and each subsequent message.")

state = load(ROOT)
if process:
    try:
        previous_rejections = len(state["rejected"])
        with st.spinner("Reading sources and validating the plan"):
            status, state = run(uploaded.getvalue().decode("utf-8"), ROOT)
        if status == "duplicate":
            st.info("This file was already processed. The existing plan was kept.")
        elif status == "complete" and len(state["rejected"]) > previous_rejections:
            st.warning("A change was rejected. See Rejections for the reason.")
        elif status == "complete":
            st.success("Messages processed. The active plan is valid.")
        else:
            st.warning(RUN_LABELS.get(status, label(status)))
    except (OSError, RuntimeError, ValueError) as exc:
        st.error(str(exc))

trace = state["trace"]
token_totals = totals(trace)
last_run = next((item for item in reversed(trace) if item["type"] == "run_summary"), None)
with st.container(horizontal=True, vertical_alignment="center"):
    st.badge("Plan valid" if state["plan"] else "Awaiting valid plan",
             color="green" if state["plan"] else "gray",
             icon=":material/check_circle:" if state["plan"] else ":material/pending:")
    if last_run:
        run_status = last_run["status"]
        st.badge(RUN_LABELS.get(run_status, label(run_status)),
                 color="gray" if run_status in {"complete", "duplicate"} else "orange")
    if state["plan"]:
        st.download_button("Download plan.json", encode(plan_document(state)), "plan.json",
                           "application/json", icon=":material/download:", on_click="ignore", key="plan_export")
    if trace:
        st.download_button("Download trace.json", encode(trace + [token_totals]), "trace.json",
                           "application/json", icon=":material/download:", on_click="ignore", key="trace_export")

overview_tab, plan_tab, trace_tab, changes_tab = st.tabs(
    ["Overview", "Event plan", "Trace", f"Rejections ({len(state['rejected'])})"],
    key="views", on_change="rerun",
)
if overview_tab.open:
    with overview_tab:
        overview(state, token_totals, last_run)
if plan_tab.open:
    with plan_tab:
        plan_view(state)
if trace_tab.open:
    with trace_tab:
        trace_view(trace, token_totals)
if changes_tab.open:
    with changes_tab:
        changes_view(state)
