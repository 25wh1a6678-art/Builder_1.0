from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.planner import (
    ARCHETYPE_BUDGETS,
    CompanyArchetype,
    CompanyResearchState,
    RequestDeduplicator,
    calculate_expected_yield,
    classify_company_archetype,
    plan_company_research,
    should_early_stop,
)


class TestV8BudgetPlanner(unittest.TestCase):

    # 1. High-confidence core facts but missing external footprint
    def test_high_confidence_core_facts_missing_external_footprint_does_not_stop(self):
        state = CompanyResearchState(
            organisation_number="916340257",
            company_archetype="commercial_operating_company",
            budget_total=10,
            budget_used=5,
            budget_remaining=5,
            fields_already_supported=["identity", "legal_form", "address", "financials", "roles"],
            fields_missing=[],
            fields_not_applicable=[],
            external_observations_found=0,
            external_observations_missing=["official_website_footprint", "job_workforce_signals"],
            expected_yields={"official_website_footprint": 0.65, "job_workforce_signals": 0.45},
        )
        can_stop, reason = should_early_stop(state)
        # CRUCIAL: Must NOT stop merely because core is covered when external targets remain
        self.assertFalse(can_stop)
        self.assertIsNone(reason)

    # 2. High-confidence profile with no applicable external sources
    def test_high_confidence_profile_no_applicable_external_sources_stops(self):
        state = CompanyResearchState(
            organisation_number="925800023",
            company_archetype="residential_housing_association",
            budget_total=3,
            budget_used=2,
            budget_remaining=1,
            fields_already_supported=["identity", "legal_form", "address"],
            fields_missing=[],
            fields_not_applicable=["website_crawl", "workforce_external_jobs", "reviews_local_presence", "financials"],
            external_observations_found=0,
            external_observations_missing=["official_website_footprint", "job_workforce_signals"],
            expected_yields={"official_website_footprint": 0.0, "job_workforce_signals": 0.0},
        )
        can_stop, reason = should_early_stop(state)
        self.assertTrue(can_stop)
        self.assertIn("Core targets satisfied and applicable external targets either resolved, not applicable", reason)

    # 3. Low-confidence core field triggers further research
    def test_low_confidence_core_field_triggers_research(self):
        state = CompanyResearchState(
            organisation_number="123456789",
            company_archetype="commercial_operating_company",
            budget_total=10,
            budget_used=3,
            budget_remaining=7,
            fields_already_supported=["identity"],
            fields_missing=["address", "legal_form"],
            external_observations_missing=["official_website_footprint"],
        )
        can_stop, reason = should_early_stop(state)
        self.assertFalse(can_stop)

    # 4. Expected-yield ranking
    def test_expected_yield_ranking(self):
        profile = {
            "organisation_number": "916340257",
            "name": "WYSSEN NORGE AS",
            "legal_form": "AS",
            "website": "www.wyssen.no",
            "employees": 5,
        }
        plan = plan_company_research(profile)
        # Selected paths must be sorted by expected yield descending
        yields = [plan.expected_yields[p] for p in plan.paths_selected]
        self.assertEqual(yields, sorted(yields, reverse=True))
        # Top path should have highest expected yield
        self.assertGreater(plan.expected_yields[plan.paths_selected[0]], 0.50)

    # 5. Diminishing returns
    def test_diminishing_returns(self):
        ey_fresh = calculate_expected_yield(
            "workforce_external_jobs",
            CompanyArchetype.COMMERCIAL_OPERATING,
            has_website=True,
            is_commercial=True,
            field_covered=False,
            field_not_applicable=False,
            consecutive_zero_yields=0,
        )
        ey_diminished = calculate_expected_yield(
            "workforce_external_jobs",
            CompanyArchetype.COMMERCIAL_OPERATING,
            has_website=True,
            is_commercial=True,
            field_covered=False,
            field_not_applicable=False,
            consecutive_zero_yields=3,
        )
        # 3 consecutive zero-yield runs should penalize expected yield without blacklisting
        self.assertGreater(ey_fresh, ey_diminished)
        self.assertGreater(ey_diminished, 0.0)

    # 6. Archetype-specific budgets
    def test_archetype_specific_budgets(self):
        self.assertEqual(ARCHETYPE_BUDGETS[CompanyArchetype.COMMERCIAL_OPERATING], 10)
        self.assertEqual(ARCHETYPE_BUDGETS[CompanyArchetype.HOLDING_INVESTMENT], 4)
        self.assertEqual(ARCHETYPE_BUDGETS[CompanyArchetype.RESIDENTIAL_HOUSING], 3)
        self.assertEqual(ARCHETYPE_BUDGETS[CompanyArchetype.SMALL_OR_DORMANT], 4)

        # Plan reflects archetype budget
        housing_profile = {"organisation_number": "925800023", "name": "SAMEIET", "legal_form": "ESEK"}
        plan_housing = plan_company_research(housing_profile)
        self.assertEqual(plan_housing.budget_total, 3)

        commercial_profile = {"organisation_number": "916340257", "name": "WYSSEN AS", "legal_form": "AS", "website": "www.wyssen.no"}
        plan_comm = plan_company_research(commercial_profile)
        self.assertEqual(plan_comm.budget_total, 10)

    # 7. Request deduplication
    def test_request_deduplication(self):
        dedup = RequestDeduplicator()
        url = "https://data.brreg.no/api/enheter/916340257"
        self.assertFalse(dedup.is_redundant(url))
        dedup.record_outcome(url, "available", response={"name": "WYSSEN"})
        self.assertTrue(dedup.is_redundant(url))
        self.assertEqual(dedup.deduplicated_count, 1)

    # 8. Cached blocked source
    def test_cached_blocked_source_avoids_requests(self):
        dedup = RequestDeduplicator()
        url = "https://blocked.example.com"
        dedup.record_outcome(url, "blocked")
        self.assertTrue(dedup.is_blocked(url))

        profile = {"organisation_number": "999", "name": "BLOCKED AS", "website": url}
        plan = plan_company_research(profile, deduplicator=dedup)
        self.assertNotIn("website_crawl", plan.paths_selected)
        self.assertIn("website_crawl", plan.paths_skipped)
        self.assertGreater(plan.deduplicated_requests, 0)

    # 9. Cached not-applicable source
    def test_cached_not_applicable_source(self):
        dedup = RequestDeduplicator()
        url = "https://notapplicable.example.com"
        dedup.record_outcome(url, "not_applicable")
        self.assertTrue(dedup.is_not_applicable(url))
        self.assertTrue(dedup.is_redundant(url))

    # 10. Budget exhaustion
    def test_budget_exhaustion(self):
        state = CompanyResearchState(
            organisation_number="123",
            company_archetype="commercial_operating_company",
            budget_total=5,
            budget_used=5,
            budget_remaining=0,
            fields_missing=["some_field"],
        )
        can_stop, reason = should_early_stop(state)
        self.assertTrue(can_stop)
        self.assertEqual(reason, "Research budget exhausted.")

    # 11. External evidence priority over unnecessary duplicate core research
    def test_external_evidence_priority_over_duplicate_core_research(self):
        # Fresh external website crawl on commercial company vs duplicate query on covered core fact
        ey_external = calculate_expected_yield(
            "website_crawl",
            CompanyArchetype.COMMERCIAL_OPERATING,
            has_website=True,
            is_commercial=True,
            field_covered=False,
            field_not_applicable=False,
            estimated_cost=2,
        )
        ey_duplicate_core = calculate_expected_yield(
            "registry_live",
            CompanyArchetype.COMMERCIAL_OPERATING,
            has_website=True,
            is_commercial=True,
            field_covered=True,  # Already covered
            field_not_applicable=False,
            estimated_cost=1,
        )
        # External path to acquire new observations should have higher yield than querying already covered core
        self.assertGreater(ey_external, ey_duplicate_core)

    # 12. Source authority versus request cost
    def test_source_authority_versus_request_cost(self):
        ey_statutory = calculate_expected_yield(
            "financials",
            CompanyArchetype.COMMERCIAL_OPERATING,
            has_website=True,
            is_commercial=True,
            field_covered=False,
            field_not_applicable=False,
            estimated_cost=1,
        )
        ey_secondary_costly = calculate_expected_yield(
            "reviews_local_presence",
            CompanyArchetype.SMALL_OR_DORMANT,
            has_website=False,
            is_commercial=False,
            field_covered=False,
            field_not_applicable=False,
            estimated_cost=2,
        )
        self.assertGreater(ey_statutory, ey_secondary_costly)

    # 13. Deterministic planner output
    def test_deterministic_planner_output(self):
        profile = {
            "organisation_number": "985589003",
            "name": "ARKITEKTFIRMA JON VIKØREN AS",
            "legal_form": "AS",
            "industry_code": "71.111",
            "employees": 4,
        }
        plan1 = plan_company_research(profile)
        plan2 = plan_company_research(profile)
        self.assertEqual(plan1.paths_selected, plan2.paths_selected)
        self.assertEqual(plan1.paths_skipped, plan2.paths_skipped)
        self.assertEqual(plan1.expected_yields, plan2.expected_yields)
        self.assertEqual(plan1.budget_total, plan2.budget_total)

    # 14. No research after applicable targets are satisfied
    def test_no_research_after_applicable_targets_are_satisfied(self):
        state = CompanyResearchState(
            organisation_number="916340257",
            company_archetype="commercial_operating_company",
            budget_total=10,
            budget_used=6,
            budget_remaining=4,
            fields_already_supported=["identity", "legal_form", "address", "financials", "roles"],
            fields_missing=[],
            fields_not_applicable=["reviews_local_presence"],
            external_observations_found=2,
            external_observations_missing=[],  # All satisfied!
        )
        can_stop, reason = should_early_stop(state)
        self.assertTrue(can_stop)

    # 15. No false stopping merely because core confidence is high
    def test_no_false_stopping_merely_because_core_confidence_is_high(self):
        # Core confidence is 1.0, but website footprint and job openings remain unresolved on an active commercial AS
        state = CompanyResearchState(
            organisation_number="888567232",
            company_archetype="commercial_operating_company",
            budget_total=10,
            budget_used=4,
            budget_remaining=6,
            fields_already_supported=["identity", "legal_form", "address", "financials", "statutory_workforce"],
            fields_missing=[],
            fields_not_applicable=[],
            external_observations_found=0,
            external_observations_missing=["official_website_footprint", "job_workforce_signals"],
            expected_yields={"official_website_footprint": 0.70, "job_workforce_signals": 0.50},
        )
        can_stop, reason = should_early_stop(state)
        self.assertFalse(can_stop, "Should continue research for applicable external footprint targets.")


if __name__ == "__main__":
    unittest.main()
