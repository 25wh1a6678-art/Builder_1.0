from __future__ import annotations

import unittest
from norway_company_agent.workforce import (
    _entity_match,
    extract_official_registry_workforce,
    extract_website_career_signals,
    research_workforce_and_jobs,
)


class TestWorkforceResearch(unittest.TestCase):
    def test_entity_match_exact_and_rejection(self):
        # Exact match
        is_match, ratio, overlap = _entity_match("WYSSEN NORGE AS", "Wyssen Norge AS")
        self.assertTrue(is_match)
        self.assertGreaterEqual(ratio, 0.8)

        # Mismatch (different company)
        is_match, ratio, overlap = _entity_match("DNB BANK ASA", "Wyssen Norge AS")
        self.assertFalse(is_match)
        self.assertLess(ratio, 0.5)

    def test_extract_official_registry_workforce_with_count(self):
        profile = {
            "organisation_number": "916340257",
            "employees": 6,
            "evidence": {
                "registry": {
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "content_sha256": "hash123",
                    "value": {
                        "harRegistrertAntallAnsatte": "true",
                        "antallAnsatte": "6",
                        "registreringsdatoAntallAnsatteEnhetsregisteret": "2025-06-12",
                    },
                },
                "locations": {
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=916340257",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "content_sha256": "hash456",
                    "value": {
                        "locations": [
                            {"name": "WYSSEN NORGE AS AVD SOGNDAL"},
                            {"name": "WYSSEN NORGE AS AVD TROMSØ"},
                        ]
                    },
                },
            },
        }
        signals = extract_official_registry_workforce(profile)
        self.assertEqual(len(signals), 2)
        count_sig = next(s for s in signals if s["type"] == "official_workforce_count")
        self.assertEqual(count_sig["value"], 6)
        loc_sig = next(s for s in signals if s["type"] == "registered_workplaces")
        self.assertEqual(loc_sig["value"], 2)

    def test_extract_website_career_signals(self):
        profile = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "http://www.aelektronikk.no/",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "value": {
                        "identity_assessment": {"status": "exact", "publishable": True},
                        "pages": [
                            {"url": "http://www.aelektronikk.no/karriere", "title": "Karriere", "main_text_excerpt": "Vi søker ny ingeniør!"},
                        ],
                    },
                }
            },
        }
        signals, rej_ent, rej_ev = extract_website_career_signals(profile)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["type"], "website_hiring_signal")
        self.assertEqual(rej_ent, 0)
        self.assertEqual(rej_ev, 0)

    def test_research_workforce_and_jobs_end_to_end(self):
        profile = {
            "organisation_number": "916340257",
            "name": "WYSSEN NORGE AS",
            "employees": 6,
            "evidence": {
                "registry": {
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    "value": {"harRegistrertAntallAnsatte": "true"},
                },
                "locations": {"value": {"locations": []}},
                "website": {"status": "not_found"},
            },
        }
        wf_ev, metrics = research_workforce_and_jobs(profile)
        self.assertIn("workforce", profile)
        self.assertIn("workforce", profile["evidence"])
        self.assertEqual(wf_ev["status"], "available")
        self.assertEqual(metrics["workforce_signals_count"], 1)
        self.assertEqual(profile["workforce"]["organisation_number"], "916340257")


if __name__ == "__main__":
    unittest.main()
