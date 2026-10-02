from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.official import serve_snapshot_registry_live, snapshot_normalized_entity
from norway_company_agent.planner import (
    CompanyArchetype,
    classify_company_archetype,
    plan_company_research,
)
from norway_company_agent.evidence import evidence


class TestV10BulkBaseline(unittest.TestCase):
    def setUp(self):
        self.sample_profile = {
            "organisation_number": "985589003",
            "name": "ARKITEKTFIRMA JON VIKØREN AS",
            "legal_form": "AS",
            "employees": None,
            "bankrupt": False,
            "liquidating": False,
            "municipality": "VIK",
            "municipality_number": "4639",
            "industry_code": "71.110",
            "industry_label": "Arkitektvirksomhet",
            "website": "",
            "latest_submitted_accounts": "2025",
            "evidence": {
                "registry": evidence(
                    "registry",
                    "available",
                    "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    value={
                        "organisasjonsnummer": "985589003",
                        "navn": "ARKITEKTFIRMA JON VIKØREN AS",
                        "organisasjonsform.kode": "AS",
                        "forretningsadresse.kommune": "VIK",
                        "forretningsadresse.postnummer": "6893",
                        "forretningsadresse.adresse": "Tomtebu 2",
                        "forretningsadresse.poststed": "VIK I SOGN",
                        "sisteInnsendteAarsregnskap": "2025",
                    },
                    retrieved_at="2026-10-02T12:00:00Z",
                    content_sha256="abc123hash",
                    source_row_key="985589003",
                )
            },
        }

    def test_snapshot_normalized_entity_extraction(self):
        norm = snapshot_normalized_entity(self.sample_profile)
        self.assertEqual(norm["organisation_number"], "985589003")
        self.assertEqual(norm["name"], "ARKITEKTFIRMA JON VIKØREN AS")
        self.assertEqual(norm["legal_form"], "AS")
        self.assertEqual(norm["business_address"]["kommune"], "VIK")
        self.assertEqual(norm["business_address"]["postnummer"], "6893")
        self.assertEqual(norm["latest_submitted_accounts"], "2025")

    def test_serve_snapshot_registry_live_provenance(self):
        ev = serve_snapshot_registry_live(self.sample_profile)
        self.assertEqual(ev["field"], "registry_live")
        self.assertEqual(ev["status"], "available")
        self.assertEqual(ev["source_type"], "statutory_registry_snapshot")
        self.assertEqual(ev["retrieved_at"], "2026-10-02T12:00:00Z")
        self.assertEqual(ev["content_sha256"], "abc123hash")
        self.assertIn("Tier 1", ev.get("note", ""))

    def test_planner_skips_redundant_registry_live_with_bulk_baseline(self):
        plan = plan_company_research(self.sample_profile, use_bulk_baseline=True)
        self.assertFalse(plan.should_execute("registry_live"))
        self.assertIn("registry_live", plan.paths_skipped)
        self.assertIn("fully satisfied by local statutory registry snapshot", plan.skip_reasons["registry_live"])
        self.assertGreater(plan.estimated_requests_saved, 0)

    def test_planner_preserves_external_research_for_commercial_operating(self):
        comm_profile = dict(self.sample_profile)
        comm_profile["website"] = "https://www.arkitekt-vikoren.no"
        comm_profile["employees"] = 4
        plan = plan_company_research(comm_profile, use_bulk_baseline=True)
        self.assertEqual(plan.archetype, CompanyArchetype.COMMERCIAL_OPERATING)
        self.assertTrue(plan.should_execute("website_crawl"))
        self.assertTrue(plan.should_execute("workforce_external_jobs"))
        self.assertTrue(plan.should_execute("reviews_local_presence"))
        self.assertTrue(plan.should_execute("financials"))

    def test_planner_defers_external_noise_for_dormant_and_housing(self):
        housing_profile = dict(self.sample_profile)
        housing_profile["legal_form"] = "BRL"
        housing_profile["name"] = "VIK BORETTSLAG"
        housing_profile["industry_code"] = "97.001"
        plan = plan_company_research(housing_profile, use_bulk_baseline=True)
        self.assertEqual(plan.archetype, CompanyArchetype.RESIDENTIAL_HOUSING)
        self.assertFalse(plan.should_execute("workforce_external_jobs"))
        self.assertFalse(plan.should_execute("reviews_local_presence"))
        self.assertFalse(plan.should_execute("financials"))
        self.assertFalse(plan.should_execute("group"))
