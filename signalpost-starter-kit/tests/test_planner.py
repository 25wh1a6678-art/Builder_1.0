from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.planner import (
    BatchPlannerTelemetry,
    CompanyArchetype,
    classify_company_archetype,
    plan_company_research,
)


class TestAdaptiveResearchPlanner(unittest.TestCase):
    def test_classify_residential_housing_association(self):
        profile = {
            "organisation_number": "925800023",
            "name": "SAMEIET LENSMANNSTUNET 1",
            "legal_form": "ESEK",
            "industry_code": "97.001",
        }
        archetype, reasons = classify_company_archetype(profile)
        self.assertEqual(archetype, CompanyArchetype.RESIDENTIAL_HOUSING)
        self.assertTrue(len(reasons) > 0)

        plan = plan_company_research(profile)
        self.assertFalse(plan.should_execute("workforce_external_jobs"))
        self.assertFalse(plan.should_execute("reviews_local_presence"))
        self.assertFalse(plan.should_execute("group"))
        self.assertGreater(plan.estimated_requests_saved, 0)
        self.assertTrue(plan.routing_adapted)

    def test_classify_holding_company(self):
        profile = {
            "organisation_number": "935095190",
            "name": "FJELLGLØD HOLDING AS",
            "legal_form": "AS",
            "industry_code": "64.201",
        }
        archetype, reasons = classify_company_archetype(profile)
        self.assertEqual(archetype, CompanyArchetype.HOLDING_INVESTMENT)

        plan = plan_company_research(profile)
        self.assertFalse(plan.should_execute("workforce_external_jobs"))
        self.assertFalse(plan.should_execute("reviews_local_presence"))
        self.assertTrue(plan.should_execute("group"))
        self.assertTrue(plan.routing_adapted)

    def test_classify_commercial_operating_company(self):
        profile = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "legal_form": "AS",
            "industry_code": "71.129",
            "website": "www.aelektronikk.no",
        }
        archetype, reasons = classify_company_archetype(profile)
        self.assertEqual(archetype, CompanyArchetype.COMMERCIAL_OPERATING)

        plan = plan_company_research(profile)
        self.assertTrue(plan.should_execute("website_crawl"))
        self.assertTrue(plan.should_execute("reviews_local_presence"))
        self.assertTrue(plan.should_execute("workforce_external_jobs"))

    def test_batch_planner_telemetry(self):
        telemetry = BatchPlannerTelemetry()
        housing_profile = {
            "organisation_number": "982669251",
            "name": "BONDELIA I BORETTSLAG",
            "legal_form": "BRL",
            "industry_code": "97.001",
        }
        plan = plan_company_research(housing_profile)
        telemetry.record_plan(plan)
        telemetry.record_run_metrics(requests=4, cache_hits=1, useful_observations=0)

        summary = telemetry.summary()
        self.assertEqual(summary["companies_processed"], 1)
        self.assertEqual(summary["companies_with_adapted_routing"], 1)
        self.assertGreater(summary["requests_saved"], 0)
        self.assertEqual(summary["cache_hits"], 1)
        self.assertIn("workforce_external_jobs", "".join(summary["skip_reasons"].keys()))
