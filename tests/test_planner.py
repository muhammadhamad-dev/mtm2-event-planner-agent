import unittest
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from constraintos.engine import apply_message, grounded_date, grounded_messages, merge, older, quote_span
from constraintos.planner import build
from constraintos.schema import Fact, Message
from constraintos.state import load, save


def rows(**values):
    return {key.replace("__", "."): {"key": key.replace("__", "."), "value": value}
            for key, value in values.items()}


def base():
    return rows(
        venue__options__studio__capacity=44,
        venue__options__studio__cost=25,
        venue__options__annex__capacity=60,
        venue__options__annex__cost=40,
        venue__options__annex__features=["step_free"],
        schedule__event_minutes=105,
        schedule__activities__meal__start=35,
        schedule__activities__meal__minutes=17,
        food__options__plates__unit_cost=2.25,
        judging__students=30,
        judging__judges=3,
        judging__staff=4,
        judging__team_size=5,
        judging__minutes_per_team=12,
        judging__max_teams_per_judge=3,
        judging__honorarium=9,
        budget__cap=180,
        budget__commitments__signage=7.50,
    )


class PlannerTests(unittest.TestCase):
    def test_dates_require_grounded_day_month_and_year(self):
        cases = [
            ("2023-11-09", "Organizer, email\n9 Nov", "--11-09"),
            ("2024-11-09", "9 November 2026", "2026-11-09"),
            ("2025-07-14", "14 Jul, project code 2025", "--07-14"),
            ("", "Sent: 2026-03-04", "2026-03-04"),
            ("2025-03-04", "4/3/2025", "2025-03-04"),
            ("2025-03-04", "No timestamp was provided.", ""),
            ("2025-03-04", "Posted 4 March 2024 or 4 March 2025", ""),
        ]
        for guess, source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(grounded_date(guess, source), expected)
        self.assertTrue(older("--09-12", "--10-13"))
        self.assertFalse(older("--09-12", "2023-10-13"))
        self.assertTrue(older("2023-12-30", "2024-01-02"))

    def test_message_grounding_uses_posting_metadata_and_preserves_original_body(self):
        body = "We can reuse the signs from 6 May 2024."
        raw = "Finance, 18 Apr\r\n" + body
        content = json.dumps({"messages": [{
            "evidence": body, "source": "Finance", "date": "2024-04-18",
            "disposition": "irrelevant", "reason": "", "facts": [],
        }]})
        message, = grounded_messages(raw, content)
        self.assertEqual(message.date, "--04-18")
        self.assertEqual(message.evidence, body)
        self.assertIn("2024-04-18", content)

    def test_tentative_availability_cannot_change_judge_count(self):
        for notice in ("I might arrive after the first round.", "Main shayad der se pohnchun."):
            with self.subTest(notice=notice):
                facts = base()
                plan, errors = build(facts, [])
                self.assertFalse(errors)
                state = {"facts": facts, "plan": plan, "rules": {}, "proposals": [],
                         "messages": [], "rejected": [], "trace": []}
                message = Message(
                    evidence=notice, source="Judge", date="", disposition="relevant", reason="",
                    facts=[Fact(area="judging", key="judging.judges", value=2, kind="correction",
                                authority="participant", operator="none", evidence=notice)],
                )
                apply_message(state, message, False, float("inf"), state["trace"].append)
                self.assertIs(state["plan"], plan)
                self.assertEqual(state["facts"]["judging.judges"]["value"], 3)
                self.assertFalse(state["rejected"])
                self.assertEqual(state["trace"][0]["status"], "advisory")

    def test_unapproved_proposal_uses_generic_ledger(self):
        quote = "Could we hire a photographer for 300?"
        message = Message(
            evidence=quote, source="Volunteer", date="", disposition="relevant", reason="",
            facts=[Fact(area="food", key="food.options.photographer.quote", value=300, kind="proposal",
                        authority="participant", operator="none", evidence=quote)],
        )
        facts = base()
        before, _ = build(facts, [])
        changed, proposals, _, _ = merge(message, facts, {})
        self.assertFalse(changed)
        self.assertEqual(proposals[0]["area"], "proposal")
        self.assertEqual(proposals[0]["evidence"], quote)
        self.assertEqual(build(facts, [])[0]["budget"], before["budget"])

    def test_quote_matching_preserves_windows_newlines(self):
        source = "Sender\r\nThe quote is 4.\r\nDelivered."
        left, right = quote_span(source, "The quote is 4.\nDelivered.")
        self.assertEqual(source[left:right], "The quote is 4.\r\nDelivered.")

    def test_plan_respects_extra_feature_and_exact_cost(self):
        plan, errors = build(base(), [{"key": "venue.features", "operator": "contains", "value": "step_free"}])
        self.assertFalse(errors, errors)
        self.assertEqual(plan["venue"]["selected"], "annex")
        self.assertEqual(plan["food"]["quantity"], 37)
        self.assertEqual(plan["budget"]["total"], Decimal("157.75"))
        self.assertEqual(plan["schedule"]["activities"]["judging"]["minutes"], 24)
        self.assertEqual(plan["schedule"]["timeline"][0]["start"], 0)
        self.assertEqual(plan["schedule"]["timeline"][-1]["end"], 105)
        self.assertEqual(sum(len(row["teams"]) for row in plan["judging"]["assignments"]), 6)

    def test_candidate_under_order_is_invalid(self):
        _, errors = build({**base(), **rows(food__quantity=22)}, [])
        self.assertIn("does not cover 37", errors[0])

    def test_timed_requirement_changes_schedule_placement(self):
        rules = [
            {"key": "schedule.activities.briefing.minutes", "operator": "eq", "value": 9},
            {"key": "schedule.activities.briefing.start", "operator": "ge", "value": 25},
            {"key": "schedule.activities.briefing.end", "operator": "le", "value": 35},
        ]
        plan, errors = build(base(), rules)
        self.assertFalse(errors, errors)
        self.assertEqual(plan["schedule"]["activities"]["briefing"]["start"], 25)

    def test_required_end_time_anchors_an_unfixed_schedule(self):
        rules = [{"key": "schedule.end", "operator": "eq", "value": 750}]
        plan, errors = build(base() | rows(schedule__activities__meal__start=680), rules)
        self.assertFalse(errors, errors)
        self.assertEqual(plan["schedule"]["start"], 645)
        self.assertEqual(plan["schedule"]["end"], 750)
        self.assertFalse(plan["schedule"]["relative_times"])

    def test_infeasible_update_has_specific_budget_failure(self):
        _, errors = build({**base(), **rows(budget__cap=130)}, [])
        self.assertIn("exceeds budget cap", errors[0])

    def test_rule_can_compare_fields_and_raise_the_default_order_quantity(self):
        rules = [
            {"key": "venue.capacity", "operator": "ge", "value": "$food.quantity"},
            {"key": "food.quantity", "operator": "ge", "value": 46},
        ]
        plan, errors = build({**base(), **rows(budget__cap=200)}, rules)
        self.assertFalse(errors, errors)
        self.assertEqual(plan["food"]["quantity"], 46)
        self.assertEqual(plan["venue"]["selected"], "annex")

    def test_schedule_rules_can_use_the_selected_venues_availability(self):
        facts = base() | rows(
            venue__options__studio__available_from=650,
            venue__options__annex__available_from=640,
            schedule__activities__meal__start=680,
        )
        rules = [
            {"key": "schedule.end", "operator": "eq", "value": 750},
            {"key": "schedule.start", "operator": "ge", "value": "$venue.available_from"},
        ]
        plan, errors = build(facts, rules)
        self.assertFalse(errors, errors)
        self.assertEqual(plan["venue"]["selected"], "annex")
        self.assertEqual(plan["schedule"]["start"], 645)

    def test_vendor_quote_date_and_undated_correction(self):
        def quote(value, posted, kind):
            evidence = f"Plates: {value}"
            return Message(
                evidence=evidence, source="Kitchen", date=posted, disposition="relevant",
                reason="", facts=[Fact(area="food", key="food.options.plates.unit_cost",
                                       value=value, kind=kind, authority="owner",
                                       operator="none", evidence=evidence)],
            )

        facts, rules = {}, {}
        merge(quote(2.25, "--07-15", "fact"), facts, rules)
        areas, _, notes, _ = merge(quote(3, "--07-01", "fact"), facts, rules)
        self.assertFalse(areas)
        self.assertIn("stale", notes[0])
        self.assertEqual(facts["food.options.plates.unit_cost"]["value"], 2.25)
        areas, _, _, _ = merge(quote(2.50, "", "correction"), facts, rules)
        self.assertEqual(areas, {"food"})
        self.assertIn("supersedes", facts["food.options.plates.unit_cost"])

    def test_bad_update_preserves_plan_and_recorded_rejection_survives_reload(self):
        facts = base()
        plan, errors = build(facts, [])
        self.assertFalse(errors, errors)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = load(root)
            state.update(facts=facts, plan=plan)
            evidence = "Kitchen: Order updated to 22 lunches."
            apply_message(state, Message(
                evidence=evidence, source="Kitchen", date="", disposition="relevant",
                reason="", facts=[Fact(area="food", key="food.quantity", value=22,
                                       kind="instruction", authority="owner",
                                       operator="none", evidence=evidence)],
            ), False, float("inf"), state["trace"].append)
            self.assertIs(state["plan"], plan)
            self.assertNotIn("food.quantity", state["facts"])
            save(root, state)
            reloaded = load(root)
            self.assertEqual(reloaded["plan"]["food"]["quantity"], 37)
            self.assertIn("does not cover 37", reloaded["rejected"][0]["reason"])
            self.assertTrue((root / "plan.json").is_file())
            self.assertTrue((root / "trace.json").is_file())

    def test_finance_correction_changes_budget_and_keeps_other_areas(self):
        facts = base()
        prior = facts["budget.commitments.signage"]
        prior.update(source="Finance", date="--09-05", message_id="previous")
        plan, errors = build(facts, [])
        self.assertFalse(errors, errors)
        state = {"facts": facts, "plan": plan, "rules": {}, "proposals": [],
                 "messages": [], "rejected": [], "trace": []}
        evidence = "Finance here. Signage came in at 12.50, not 7.50."
        apply_message(state, Message(
            evidence=evidence, source="Finance", date="", disposition="relevant",
            reason="", facts=[Fact(area="budget", key="budget.commitments.signage", value=12.50,
                                   kind="fact", authority="owner",
                                   operator="none", evidence=evidence)],
        ), False, float("inf"), state["trace"].append)
        self.assertFalse(state["rejected"], state["rejected"])
        self.assertEqual(state["plan"]["budget"]["total"] - plan["budget"]["total"], Decimal("5"))
        for area in ("venue", "schedule", "food", "judging"):
            self.assertEqual(state["plan"][area], plan[area])
