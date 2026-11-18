import unittest
from decimal import Decimal

from constraintos.engine import merge
from constraintos.planner import build
from constraintos.schema import Fact, Message


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

    def test_infeasible_update_has_specific_budget_failure(self):
        _, errors = build({**base(), **rows(budget__cap=130)}, [])
        self.assertIn("exceeds budget cap", errors[0])

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
        areas, _, notes = merge(quote(3, "--07-01", "fact"), facts, rules)
        self.assertFalse(areas)
        self.assertIn("stale", notes[0])
        self.assertEqual(facts["food.options.plates.unit_cost"]["value"], 2.25)
        areas, _, _ = merge(quote(2.50, "", "correction"), facts, rules)
        self.assertEqual(areas, {"food"})
        self.assertIn("supersedes", facts["food.options.plates.unit_cost"])
