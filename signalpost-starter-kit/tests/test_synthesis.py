from __future__ import annotations

import unittest
from norway_company_agent.synthesis import (
    generate_company_summary,
    generate_business_description,
    detect_financial_operational_changes,
    generate_source_backed_explanations,
    identify_explicit_unknowns,
    synthesize_company_profile,
)


class TestSynthesisEngine(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "legal_form": "AS",
            "employees": None,
            "bankrupt": False,
            "liquidating": False,
            "municipality": "ARENDAL",
            "industry_code": "71.129",
            "industry_label": "Annen teknisk konsulentvirksomhet",
            "evidence": {
                "registry": {
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    "retrieved_at": "2026-10-02T09:29:03Z",
                    "content_sha256": "abc123hash",
                    "value": {
                        "vedtektsfestetFormaal": "Teknisk konsulentvirksomhet.",
                        "aktivitet": "Teknisk konsulentvirksomhet.",
                    },
                },
                "financials": {
                    "status": "available",
                    "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/888567232",
                    "retrieved_at": "2026-10-02T09:29:35Z",
                    "content_sha256": "fin123hash",
                    "value": {
                        "records": [
                            {
                                "period": {"tilDato": "2024-12-31"},
                                "revenue": 562350.0,
                                "annual_result": 179356.0,
                                "equity": -23282.0,
                            },
                            {
                                "period": {"tilDato": "2025-12-31"},
                                "revenue": 1425713.0,
                                "annual_result": 197942.0,
                                "equity": 174660.0,
                            },
                        ],
                    },
                },
                "roles": {
                    "status": "available",
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/888567232/roller",
                    "retrieved_at": "2026-10-02T09:29:36Z",
                    "content_sha256": "role123hash",
                    "value": {
                        "roles": [
                            {"name": "Tor Ivar Aas", "role": "Daglig leder", "role_code": "DAGL", "inactive": False, "last_changed": "2021-04-09"},
                            {"name": "Tor Ivar Aas", "role": "Styrets leder", "role_code": "LEDE", "inactive": False, "last_changed": "2021-04-09"},
                        ],
                    },
                },
                "group": {"status": "not_found"},
                "website": {
                    "status": "available",
                    "source_url": "http://www.aelektronikk.no/",
                    "retrieved_at": "2026-10-02T09:29:44Z",
                    "content_sha256": "web123hash",
                    "value": {
                        "final_url": "http://www.aelektronikk.no/",
                        "description": "Tjenester innenfor Mekatronikk.",
                        "identity_assessment": {"status": "exact", "score": 0.95, "publishable": True},
                    },
                },
            },
        }

    def test_generate_company_summary(self):
        summary = generate_company_summary(self.profile)
        self.assertIn("AAS ELEKTRONIKK AS", summary)
        self.assertIn("Org. 888567232", summary)
        self.assertIn("Tor Ivar Aas", summary)
        self.assertIn("1 425 713 NOK", summary)
        self.assertIn("No registered employee count reported", summary)

    def test_generate_business_description(self):
        desc = generate_business_description(self.profile)
        self.assertIn("Tjenester innenfor Mekatronikk", desc)
        self.assertIn("Teknisk konsulentvirksomhet", desc)
        self.assertIn("NACE 71.129", desc)

    def test_financial_operational_changes(self):
        changes = detect_financial_operational_changes(self.profile)
        self.assertTrue(len(changes) >= 2)
        rev_change = next(c for c in changes if c["type"] == "revenue_trend")
        self.assertEqual(rev_change["period"], "2024 -> 2025")
        self.assertIn("+153.5%", rev_change["detail"])

    def test_source_backed_explanations(self):
        explanations = generate_source_backed_explanations(self.profile)
        subjects = [e["subject"] for e in explanations]
        self.assertIn("Company Identity & Legal Foundation", subjects)
        self.assertIn("Statutory Annual Accounts", subjects)
        self.assertIn("Public Role Holders & Governance", subjects)
        self.assertIn("Official Company Website", subjects)
        for e in explanations:
            self.assertTrue(e["source_url"])
            self.assertTrue(e["content_sha256"])

    def test_identify_explicit_unknowns(self):
        unknowns = identify_explicit_unknowns(self.profile)
        fields = [u["field"] for u in unknowns]
        self.assertIn("employees", fields)
        self.assertIn("group_structure", fields)
        emp = next(u for u in unknowns if u["field"] == "employees")
        self.assertEqual(emp["state"], "not_available")
        self.assertIn("not zero", emp["explanation"])

    def test_synthesize_company_profile(self):
        synthesize_company_profile(self.profile)
        self.assertIn("synthesis", self.profile)
        self.assertIn("synthesis", self.profile["evidence"])
        self.assertEqual(self.profile["evidence"]["synthesis"]["status"], "available")
        self.assertEqual(self.profile["evidence"]["synthesis"]["source_type"], "derived_profile_synthesis")
        self.assertTrue(self.profile["evidence"]["synthesis"]["content_sha256"])


if __name__ == "__main__":
    unittest.main()
