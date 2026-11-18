from decimal import Decimal
from html import escape
from pathlib import Path

import altair as alt
import streamlit as st

from constraintos.engine import run
from constraintos.presentation import inbox_records
from constraintos.state import encode, load, plan_document, totals

BRAND = "ConstraintOS"
ROOT = Path(".constraintos")
AREAS = ("Venue", "Schedule", "Food", "Judging", "Budget")
NAVIGATION = {
    "Overview": "space_dashboard", "Message Inbox": "inbox", "Event Plan": "event_note",
    "Agent Activity": "monitoring", "Rejected Messages": "gpp_bad", "Execution Trace": "terminal",
}
RUN_LABELS = {
    "complete": "Run complete", "duplicate": "Duplicate skipped", "incomplete": "Plan incomplete",
    "provider_error": "Provider request failed", "extraction_error": "Extraction failed",
}
EVENT_LABELS = {
    "analyzed": "Dependency analysis", "accepted": "Plan validated", "ignored": "Message ignored",
    "advisory": "Advisory recorded", "duplicate": "Duplicate skipped", "incomplete": "Plan incomplete",
    "provider_error": "Provider request failed", "extraction_error": "Extraction failed",
    "rate_limit_retry": "Rate-limit retry", "structured_output_retry": "Structured output retry",
}
st.set_page_config(page_title=BRAND, page_icon=":material/account_tree:", layout="wide",
                   initial_sidebar_state="auto")


def label(value):
    return str(value).replace("_", " ").replace("-", " ").capitalize()


def option_name(option, selected=None):
    return option.get("name") or str(selected or option.get("selected", "")).replace("_", " ").replace("-", " ").title()


def money(amount, currency):
    number = Decimal(str(amount))
    return f"{currency} {number:,.{0 if number == number.to_integral() else 2}f}".strip()


def clock(minutes, relative):
    hours, minutes = divmod(minutes, 60)
    return f"{'+' if relative else ''}{hours:02}:{minutes:02}"


def table(rows, description, **options):
    st.dataframe(rows, hide_index=True, alt=description, **options)


def status_badge(value):
    color = {
        "Rejected": "red", "Advisory": "orange", "Accepted": "green", "In active plan": "green",
        "Plan valid": "green", "Plan incomplete": "orange", "Extraction unavailable": "orange",
    }.get(value, "gray")
    st.badge(value, color=color)


def tile(title, value, detail="", tone="lime"):
    st.html(
        f'<div class="cos-tile"><div class="cos-meta">{escape(title)}</div>'
        f'<strong class="cos-{tone}">{escape(str(value))}</strong>'
        f'<div class="cos-meta">{escape(detail)}</div></div>'
    )


def evidence(state, area):
    records = [(item, "Fact") for key, item in state["facts"].items() if key.split(".")[0] == area]
    records.extend((item, "Constraint") for item in state["rules"].values()
                   if item["key"].split(".")[0] == area)
    st.subheader("Source evidence")
    if not records:
        st.caption("No source records are attached to this area.")
        return
    table([
        {"Type": kind, "Field": item["key"], "Source": item.get("source") or "Unknown",
         "Date": item.get("date") or "Unstated", "Evidence": item.get("evidence", "")}
        for item, kind in records
    ], f"Source evidence and constraints for {area}")
    with st.expander("Read complete evidence"):
        for item, kind in records:
            st.text(f"{kind} · {item.get('source') or 'Unknown'} · {item['key']}")
            st.code(item.get("evidence", ""), language=None, wrap_lines=True)


def budget_usage(budget):
    cap = Decimal(str(budget["cap"]))
    if cap > 0:
        share = Decimal(str(budget["total"])) / cap
        st.progress(float(max(0, min(1, share))), text=f"{share:.1%} of spending cap")


def plan_summary(plan):
    venue, schedule, food, judging, budget = (plan[area.lower()] for area in AREAS)
    currency = budget.get("currency") or ""
    return {
        "Venue": (option_name(venue), f"{venue['capacity']:,} seats · {venue['required_capacity']:,} needed"),
        "Schedule": (f"{schedule['event_minutes']} min", f"{len(schedule['timeline'])} scheduled blocks"),
        "Food": (f"{food['quantity']:,} meals", option_name(food)),
        "Judging": (f"{judging['judges']} judges", f"{judging['teams']} teams · {judging['waves']} rounds"),
        "Budget": (money(budget["total"], currency), f"Cap {money(budget['cap'], currency)}"),
    }


def activity_title(event):
    if event["type"] == "model_call":
        return f"Groq call {event['number']:02} · {event['area']}"
    if event["type"] == "rejection":
        return "Message rejected"
    if event["type"] == "run_summary":
        return RUN_LABELS.get(event["status"], label(event["status"]))
    if event.get("status") == "accepted" and event.get("source"):
        return "Update accepted and plan validated"
    return EVENT_LABELS.get(event.get("status"), label(event["type"]))


def event_detail(event):
    if event.get("source"):
        st.text(event["source"])
    if event.get("message"):
        st.code(event["message"], language=None, wrap_lines=True)
    if event.get("reason"):
        st.text(event["reason"])
    if event.get("changed_areas"):
        st.caption("Changed · " + ", ".join(event["changed_areas"]))
    if event.get("affected_areas"):
        st.caption("Affected · " + ", ".join(event["affected_areas"]))
    if event.get("validated_areas"):
        st.caption("Validated · " + ", ".join(event["validated_areas"]))
    if event.get("decisions") or event.get("reasons"):
        st.text("\n".join(event.get("decisions", []) + event.get("reasons", [])))
    if event.get("wall_seconds") is not None:
        st.caption(f"{event['wall_seconds']:.3f} seconds")
    if event.get("wait_seconds") is not None:
        st.caption(f"Retry wait · {event['wait_seconds']} seconds")


def overview(state, records):
    plan, trace = state["plan"], state["trace"]
    if not plan:
        st.info("Upload and process a TXT message pack to build the first plan.")
    else:
        summaries = plan_summary(plan)
        judging, budget = plan["judging"], plan["budget"]
        venue, food = plan["venue"], plan["food"]
        currency = budget.get("currency") or ""
        st.caption(f"{judging['students']:,} students · {judging['teams']:,} teams · "
                   f"{food.get('event_attendance') or venue['required_capacity']:,} expected attendees")
        with st.grid(5, min_column_width=154):
            for area, (value, detail) in summaries.items():
                tile(area, value, detail)
        left, right = st.columns([1.42, 1])
        with left, st.container(border=True):
            st.subheader("Current plan")
            table([{"Area": area, "Check": "Validated", "Decision": value, "Details": detail}
                   for area, (value, detail) in summaries.items()], "Five validated planning decisions")
            budget_usage(budget)
            st.caption(f"{money(budget['remaining'], currency)} remaining")
        with right, st.container(border=True):
            st.subheader("Recent decisions")
            for event in [item for item in reversed(trace) if item["type"] == "rejection"
                          or item.get("status") in {"accepted", "advisory"}][:3]:
                title = activity_title(event)
                if event.get("source"):
                    title += f" · {event['source']}"
                with st.expander(title):
                    event_detail(event)
                    if event.get("checks") and not event.get("source"):
                        st.caption("Plan checks are recorded in Agent Activity.")
    with st.grid(3, min_column_width=155):
        tile("Messages processed", len(state["messages"]), "Unique messages in validation")
        tile("Rejected messages", len(state["rejected"]), "See Rejected Messages",
             tone="red" if state["rejected"] else "lime")
        tile("Model calls", totals(trace)["model_calls"], "See Execution Trace")
    if records:
        with st.container(border=True):
            st.subheader("Recent message outcomes")
            table([{"From": row["source"], "Message": " ".join(row["body"].split())[:100],
                    "Outcome": row["outcome"]} for row in reversed(records[-4:])],
                  "Recent incoming messages and recorded outcomes")
    if trace:
        with st.container(border=True):
            st.subheader("Activity preview")
            table([{"Sequence": number + 1, "Event": activity_title(item)}
                   for number, item in list(enumerate(trace))[-4:]], "Latest execution steps")


def inbox_view(records):
    if not records:
        st.info("Processed messages will appear here with their source text and recorded outcome.")
        return
    rows = list(reversed(records))
    left, right = st.columns([1, 1.78])
    with left, st.container(border=True):
        st.subheader("Incoming messages")
        selection = st.radio("Select a message", range(len(rows)), key="inbox_selected",
                             format_func=lambda index: f"{len(rows) - index:02} · {rows[index]['source']} "
                                                      f"· {rows[index]['outcome']}")
    with right, st.container(border=True):
        row = rows[selection]
        status_badge(row["outcome"])
        st.text(row["source"])
        st.caption(f"Source date · {row['date'] or 'Unstated'}")
        if row["classification"]:
            st.caption(f"Model classification · {label(row['classification'])}")
        st.subheader("Original message")
        st.code(row["body"], language=None, wrap_lines=True)
        if row["facts"]:
            st.subheader("Extracted facts")
            st.caption("These records come from the model extraction.")
            table([{"Field": fact["key"], "Value": encode(fact["value"]), "Kind": label(fact["kind"]),
                    "Authority": label(fact["authority"]), "Evidence": fact["evidence"]}
                   for fact in row["facts"]], "Model-extracted facts for the selected message")
        for event in row["events"]:
            if event.get("affected_areas") or event.get("decisions") or event.get("reason"):
                with st.expander(activity_title(event)):
                    event_detail(event)


def schedule_view(schedule):
    relative = schedule.get("relative_times", True)
    if relative:
        st.caption("Times are relative to the event start.")
    rows = [
        {"Activity": row["activity"], "Start": clock(row["start"], relative),
         "End": clock(row["end"], relative), "Minutes": row["end"] - row["start"],
         "From": row["start"] - schedule["start"], "Until": row["end"] - schedule["start"],
         "Order": order}
        for order, row in enumerate(schedule["timeline"])
    ]
    if not rows:
        return
    chart = alt.Chart(alt.Data(values=rows)).mark_bar(color="#C6F36B", cornerRadius=3, size=14).encode(
        x=alt.X("From:Q", title="Minutes from start", axis=alt.Axis(tickCount=6)),
        x2="Until:Q", y=alt.Y("Activity:N", sort=alt.SortField("Order"), title=None,
                              axis=alt.Axis(labelLimit=150)),
        tooltip=["Activity:N", "Start:N", "End:N", "Minutes:Q"],
    )
    st.altair_chart(chart, height=max(165, len(rows) * 28 + 68), alt="Timeline of scheduled activities")
    table([{key: row[key] for key in ("Activity", "Start", "End", "Minutes")} for row in rows],
          "Exact schedule activity times")


def comparison(options, selected, fields, currency):
    if len(options) < 2:
        return
    with st.expander("Available alternatives"):
        rows = []
        for key, option in options.items():
            status = "Selected" if key == selected else "Unavailable" if option.get("available") is False else "Alternative"
            row = {"Option": option_name(option, key), "Selection": status}
            for field, title in fields:
                value = option.get(field)
                if value is not None and field in {"cost", "unit_cost", "fixed_cost"}:
                    value = money(value, currency)
                row[title] = "Unstated" if value is None else value
            rows.append(row)
        table(rows, "Source-provided alternative options")


def plan_view(state):
    plan = state["plan"]
    if not plan:
        st.info("Process a message pack to see the event plan.")
        return
    area = st.segmented_control("Planning area", AREAS, default="Venue", key="selected_area", wrap=True) or "Venue"
    key = area.lower()
    decision = plan[key]
    budget = plan["budget"]
    currency = budget.get("currency") or ""
    with st.container(border=True):
        st.subheader(area if key in {"schedule", "judging", "budget"} else option_name(decision))
        status_badge("Plan valid")
        if key in {"venue", "food", "budget"} and not currency:
            st.caption("Currency denomination is unavailable in the saved plan.")
        if key == "venue":
            with st.grid(3, min_column_width=135):
                tile("Capacity", decision["capacity"])
                tile("Attendance required", decision["required_capacity"])
                tile("Venue cost", money(decision["cost"], currency))
            features = decision.get("features")
            st.text("Features · " + (", ".join(map(label, features)) if isinstance(features, list)
                                     else str(features or "Unstated")))
            comparison(decision.get("options", {}), decision["selected"],
                       [("capacity", "Seats"), ("cost", "Cost")], currency)
        elif key == "schedule":
            schedule_view(decision)
        elif key == "food":
            with st.grid(4, min_column_width=130):
                tile("People to feed", decision["headcount"])
                tile("Meals ordered", decision["quantity"])
                tile("Unit price", money(decision["unit_cost"], currency))
                tile("Food total", money(decision["cost"], currency))
            comparison(decision.get("options", {}), decision["selected"], [("unit_cost", "Unit price")], currency)
        elif key == "judging":
            with st.grid(4, min_column_width=130):
                tile("Judges", decision["judges"])
                tile("Teams / capacity", f"{decision['teams']} / {decision['team_capacity']}")
                tile("Waves", decision["waves"], f"{decision['minutes_per_team']} min per team")
                tile("Judging time", f"{decision['duration']} min")
            if decision.get("assignments"):
                table([{"Judge": row["judge"], "Teams": ", ".join(map(str, row["teams"]))}
                       for row in decision["assignments"]], "Team assignments by judge")
        else:
            with st.grid(3, min_column_width=135):
                for field, title in (("cap", "Spending cap"), ("total", "Committed spend"), ("remaining", "Remaining")):
                    tile(title, money(budget[field], currency))
            budget_usage(budget)
            rows = [{"Expense": label(field), "Amount": money(value, currency)}
                    for field, value in budget["lines"].items() if field != "commitments"]
            rows.extend({"Expense": label(field), "Amount": money(value, currency)}
                        for field, value in budget.get("commitments", {}).items())
            table(rows, "Budget ledger with named commitments")
    with st.container(border=True):
        evidence(state, key)


def activity_view(trace):
    if not trace:
        st.info("Activity will appear after the first run.")
        return
    with st.container(border=True):
        st.subheader("Processing timeline")
        selected = st.dataframe(
            [{"Sequence": index, "Event": activity_title(event),
              "Executor": "Groq model" if event["type"] == "model_call" else "Python",
              "Source": event.get("source") or "—",
              "Affected areas": ", ".join(event.get("affected_areas", [])) or "—"}
             for index, event in enumerate(trace, 1)],
            hide_index=True, alt="Recorded activity in chronological order",
            selection_mode="single-row", on_select="rerun", key="activity_timeline", height=390,
        )
        index = selected.selection.rows[0] if selected.selection.rows else 0
        event = trace[index]
        st.subheader(f"{index + 1:02} · {activity_title(event)}")
        if event["type"] == "model_call":
            st.text(event["model"])
            if event.get("started_at"):
                st.caption(event["started_at"])
            st.caption("See Execution Trace for the raw request, response and token counts.")
        else:
            event_detail(event)


def rejections_view(state, records):
    if not state["rejected"]:
        with st.container(border=True):
            status_badge("Clear")
            st.subheader("No rejected messages")
            st.caption("Conflicting updates appear here with their original text and validation reason.")
        return
    has_validated = False
    validation_history = []
    for item in state["trace"]:
        if item.get("status") == "accepted":
            has_validated = True
        if item["type"] == "rejection":
            validation_history.append((item, has_validated))
    keys = ("source", "message", "reason")
    for index in reversed(range(len(state["rejected"]))):
        row = state["rejected"][index]
        with st.container(border=True):
            status_badge("Rejected")
            st.text(row.get("source") or "Unknown sender")
            st.subheader("Validation reason")
            st.text(row["reason"])
            st.subheader("Original message")
            st.code(row["message"], language=None, wrap_lines=True)
            if index < len(validation_history):
                event, previously_valid = validation_history[index]
                if previously_valid and all(row.get(key) == event.get(key) for key in keys):
                    st.caption("The previously validated plan was retained.")
            matching = [record for record in records if record["source"] == row["source"]
                        and record["body"] == row["message"] and record["outcome"] == "Rejected"]
            if len(matching) == 1:
                affected = sorted({area for event in matching[0]["events"]
                                   for area in event.get("affected_areas", [])})
                if affected:
                    st.caption("Affected · " + ", ".join(affected))


def model_status(event):
    return f"HTTP {event['status']}" if "status" in event else event.get("error", "Unavailable")


def trace_view(trace, counts):
    calls = [item for item in trace if item["type"] == "model_call"]
    if not calls:
        st.info("No model calls have been recorded.")
        return
    with st.grid(3, min_column_width=145):
        tile("Calls", counts["model_calls"])
        tile("Provider input / output", f"{counts['prompt_tokens']:,} / {counts['completion_tokens']:,}")
        tile("Total reported tokens", f"{counts['total_tokens']:,}")
    if counts["calls_without_reported_usage"]:
        st.caption(f"Usage unavailable on {counts['calls_without_reported_usage']} calls.")
    with st.container(border=True):
        st.subheader("Model-call ledger")
        rows = []
        for event in calls:
            usage = event.get("usage") or {}
            rows.append({"Call": event["number"], "Provider / model": f"Groq · {event['model']}",
                         "Area": event["area"], "Status": model_status(event),
                         "Input": usage.get("prompt_tokens"), "Output": usage.get("completion_tokens"),
                         "Total": usage.get("total_tokens"), "Latency (s)": event["latency_seconds"]})
        table(rows, "Chronological ledger of recorded Groq calls and provider-reported usage")
    for event in calls:
        details = st.expander(f"Call {event['number']:02} · {event['area']} · {model_status(event)}",
                              key=f"model_payload_{event['number']}", on_change="rerun")
        if details.open:
            with details:
                for key, title in (("input", "Exact request"), ("output", "Exact response")):
                    st.subheader(title)
                    st.code(encode(event.get(key)), language=None, wrap_lines=True, height=250)


st.html("""
<style>
.cos-brand { display:flex; align-items:center; gap:10px; padding:8px 0 17px; }
.cos-mark { width:29px; height:29px; display:grid; place-items:center; color:#C6F36B;
    border:2px solid #C6F36B; border-radius:9px; font:700 15px monospace; }
.cos-brand strong { font-size:19px; letter-spacing:-.045em; }
.cos-brand small { display:block; color:#929A91; font-size:11px; }
.cos-tile { background:#191F1B; border:1px solid #303830; border-radius:9px; min-height:94px;
    padding:12px 14px; overflow-wrap:anywhere; }
.cos-tile strong { display:block; font-size:clamp(1rem, 1.7vw, 1.32rem); letter-spacing:-.025em; margin:3px 0; }
.cos-meta { color:#A0AA9F; font-size:.76rem; line-height:1.4; }
.cos-lime { color:#C6F36B; }
.cos-red { color:#F59991; }
.st-key-workspace_navigation [role="radiogroup"], .st-key-workspace_navigation label { width:100%; }
.st-key-workspace_navigation label { padding:7px 8px; border-radius:8px; }
.st-key-workspace_navigation label:hover { background:#191F1B; }
.st-key-workspace_navigation label:has(input:checked) { background:#29361e; box-shadow:inset 2px 0 #C6F36B; }
</style>
""")
with st.sidebar:
    st.html(f'<div class="cos-brand"><div class="cos-mark" aria-hidden="true">{escape(BRAND[0])}</div>'
            f'<div><strong>{escape(BRAND)}</strong><small>Event decision intelligence</small></div></div>')
    view = st.radio("Workspace navigation", list(NAVIGATION), key="workspace_navigation",
                    label_visibility="collapsed",
                    format_func=lambda name: f":material/{NAVIGATION[name]}: {name}")

state = load(ROOT)
st.title("Event Operations" if view == "Overview" else view)
with st.container(horizontal=True, vertical_alignment="center"):
    plan_status = st.empty()
    with st.popover("Upload TXT", icon=":material/upload_file:"):
        uploaded = st.file_uploader("Event messages", type=["txt"], key="event_messages")
        st.caption("Use this upload for both the first pack and later updates.")
        process = st.button("Process messages", type="primary", disabled=uploaded is None,
                            key="process_messages")
    export_plan = st.empty()
    export_trace = st.empty()

if process:
    try:
        previous_rejections = len(state["rejected"])
        with st.spinner("Reading sources and validating the plan"):
            status, state = run(uploaded.getvalue().decode("utf-8"), ROOT)
        if status == "duplicate":
            st.info("This file was already processed. The existing plan was kept.")
        elif status == "complete" and len(state["rejected"]) > previous_rejections:
            st.warning("A change was rejected. See Rejected Messages for the reason.")
        elif status == "complete":
            st.success("Messages processed. The active plan is valid.")
        else:
            st.warning(RUN_LABELS.get(status, label(status)))
    except UnicodeDecodeError:
        st.error("Save the message file as UTF-8 and upload it again.")
    except (OSError, RuntimeError, ValueError) as exc:
        st.error(str(exc))

trace = state["trace"]
counts = totals(trace)
with plan_status:
    status_badge("Plan valid" if state["plan"] else "Awaiting valid plan")
if state["plan"]:
    export_plan.download_button("Download plan.json", encode(plan_document(state)), "plan.json",
                                "application/json", icon=":material/download:", on_click="ignore")
if trace:
    export_trace.download_button("Download trace.json", encode(trace + [counts]), "trace.json",
                                 "application/json", icon=":material/download:", on_click="ignore")
last_run = next((item for item in reversed(trace) if item["type"] == "run_summary"), None)
if last_run and view == "Overview":
    with st.container(horizontal=True):
        st.caption(f"{RUN_LABELS.get(last_run['status'], label(last_run['status']))} · {last_run['wall_seconds']:.3f} s")
        st.caption(f"{counts['total_tokens']:,} total reported tokens")
if view == "Overview":
    overview(state, inbox_records(state))
elif view == "Message Inbox":
    inbox_view(inbox_records(state))
elif view == "Event Plan":
    plan_view(state)
elif view == "Agent Activity":
    activity_view(trace)
elif view == "Rejected Messages":
    rejections_view(state, inbox_records(state))
else:
    trace_view(trace, counts)
