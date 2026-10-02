from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.conflict_resolution import SourceFactVariant
from norway_company_agent.temporal import (
    ChangeEvent,
    FactHistory,
    TemporalProfile,
    build_company_temporal_profile,
    resolve_temporal_fact,
)


class TestTemporalEngine(unittest.TestCase):

    # 1. Same value from multiple dates
    def test_same_value_from_multiple_dates(self):
        v1 = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/regnskap/2023",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2023-12-31",
            authority=1.0,
            evidence_span="2023 filing: employees = 5",
        )
        v2 = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/regnskap/2024",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-12-31",
            authority=1.0,
            evidence_span="2024 filing: employees = 5",
        )
        history = resolve_temporal_fact("employees", [v1, v2])
        self.assertEqual(history.current_value, 5)
        self.assertEqual(history.temporal_status, "current")
        # Same value -> NO false change events
        self.assertEqual(len(history.detected_changes), 0)
        self.assertEqual(len(history.history), 2)
        # 2023 is historical, 2024 is current
        self.assertEqual(history.history[0]["temporal_status"], "historical")
        self.assertEqual(history.history[1]["temporal_status"], "current")

    # 2. Historical value followed by newer value
    def test_historical_value_followed_by_newer_value(self):
        v1 = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/regnskap/2023",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2023-12-31",
            authority=1.0,
            evidence_span="2023 filing: employees = 5",
        )
        v2 = SourceFactVariant(
            value=7,
            source_url="https://data.brreg.no/regnskap/2024",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-12-31",
            authority=1.0,
            evidence_span="2024 filing: employees = 7",
        )
        history = resolve_temporal_fact("employees", [v1, v2])
        self.assertEqual(history.current_value, 7)
        self.assertEqual(history.temporal_status, "current")
        self.assertEqual(len(history.detected_changes), 1)
        ch = history.detected_changes[0]
        self.assertEqual(ch["field"], "employees")
        self.assertEqual(ch["old_value"], 5)
        self.assertEqual(ch["new_value"], 7)
        self.assertEqual(ch["change_type"], "temporal_change")
        self.assertEqual(ch["old_date"], "2023-12-31")
        self.assertEqual(ch["new_date"], "2024-12-31")

    # 3. Newer source conflicting with older source
    def test_newer_source_conflicting_with_older_source(self):
        v1 = SourceFactVariant(
            value=1000000,
            source_url="https://data.brreg.no/regnskap/2022",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2022-12-31",
            authority=1.0,
            evidence_span="2022 revenue = 1,000,000",
        )
        v2 = SourceFactVariant(
            value=1500000,
            source_url="https://data.brreg.no/regnskap/2023",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2023-12-31",
            authority=1.0,
            evidence_span="2023 revenue = 1,500,000",
        )
        history = resolve_temporal_fact("revenue", [v1, v2])
        self.assertEqual(history.current_value, 1500000)
        self.assertEqual(len(history.detected_changes), 1)
        self.assertEqual(history.detected_changes[0]["old_value"], 1000000)
        self.assertEqual(history.detected_changes[0]["new_value"], 1500000)
        self.assertEqual(history.history[0]["temporal_status"], "historical")

    # 4. Same-date conflicting sources
    def test_same_date_conflicting_sources_not_temporal_change(self):
        # Registry says 5 (authority 1.0), website says 8 (authority 0.85) for the same timeframe
        v_registry = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            source_type="official_registry_bulk",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-05-01",
            authority=1.0,
            evidence_span="Aa-registeret registered employee count: 5",
        )
        v_website = SourceFactVariant(
            value=8,
            source_url="https://wyssenavalanche.com/about",
            source_type="company_owned",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-05-01",
            authority=0.85,
            evidence_span="Our team consists of 8 specialists",
        )
        history = resolve_temporal_fact("employees", [v_registry, v_website])
        # Statutory authority takes precedence
        self.assertEqual(history.current_value, 5)
        # CRUCIAL: Must NOT be classified as a temporal change
        self.assertEqual(len(history.detected_changes), 0)
        # Website variant preserved as conflicting_current
        self.assertEqual(len(history.history), 2)
        website_hist = next(h for h in history.history if h["source_type"] == "company_owned")
        self.assertEqual(website_hist["temporal_status"], "conflicting_current")

    # 5. Missing publication date
    def test_missing_publication_date(self):
        v = SourceFactVariant(
            value="ACTIVE",
            source_url="https://data.brreg.no/enhetsregisteret",
            source_type="official_registry_live",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-01-01",
            publication_date=None,  # explicitly missing
            authority=1.0,
        )
        history = resolve_temporal_fact("status", [v])
        self.assertEqual(history.current_value, "ACTIVE")
        self.assertIsNone(history.history[0]["publication_date"])
        self.assertEqual(history.history[0]["effective_date"], "2024-01-01")

    # 6. Retrieval date only
    def test_retrieval_date_only_no_date_fabrication(self):
        v = SourceFactVariant(
            value="NACE 71.120",
            source_url="https://data.brreg.no/enhetsregisteret",
            source_type="official_registry_live",
            retrieved_at="2026-10-02T12:34:56Z",
            effective_date=None,
            publication_date=None,
            authority=1.0,
        )
        history = resolve_temporal_fact("industry", [v])
        self.assertEqual(history.current_value, "NACE 71.120")
        self.assertIsNone(history.history[0]["effective_date"])
        self.assertIsNone(history.history[0]["publication_date"])
        self.assertEqual(history.history[0]["retrieved_at"], "2026-10-02T12:34:56Z")

    # 7. Stale source vs fresh authoritative source
    def test_stale_source_vs_fresh_authoritative_source(self):
        v_stale = SourceFactVariant(
            value="Ola Nordmann",
            source_url="https://directory.example.no/company/123",
            source_type="company_directory",
            retrieved_at="2020-01-01T00:00:00Z",
            effective_date="2020-01-01",
            authority=0.65,
            evidence_span="Directory listing: CEO Ola Nordmann",
        )
        v_fresh = SourceFactVariant(
            value="Kari Nordmann",
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/123/roller",
            source_type="official_roles",
            retrieved_at="2024-06-01T00:00:00Z",
            effective_date="2024-06-01",
            authority=1.0,
            evidence_span="BRREG DAGL registered role: Kari Nordmann",
        )
        history = resolve_temporal_fact("ceo", [v_stale, v_fresh])
        self.assertEqual(history.current_value, "Kari Nordmann")
        self.assertEqual(len(history.detected_changes), 1)
        self.assertEqual(history.detected_changes[0]["old_value"], "Ola Nordmann")
        self.assertEqual(history.detected_changes[0]["new_value"], "Kari Nordmann")

    # 8. Historical annual report vs current registry
    def test_historical_annual_report_vs_current_registry(self):
        v_report = SourceFactVariant(
            value=10,
            source_url="https://data.brreg.no/regnskap/2022",
            source_type="official_financials",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2022-12-31",
            authority=1.0,
            evidence_span="2022 annual report employee count: 10",
        )
        v_current_reg = SourceFactVariant(
            value=14,
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            source_type="official_registry_bulk",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-05-01",
            authority=1.0,
            evidence_span="Aa-registeret registered employee count: 14",
        )
        history = resolve_temporal_fact("employees", [v_report, v_current_reg])
        self.assertEqual(history.current_value, 14)
        self.assertEqual(len(history.detected_changes), 1)
        self.assertEqual(history.detected_changes[0]["old_value"], 10)
        self.assertEqual(history.detected_changes[0]["new_value"], 14)
        self.assertEqual(history.history[0]["temporal_status"], "historical")

    # 9. Current website conflict with statutory registry
    def test_current_website_conflict_with_statutory_registry(self):
        v_statutory = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            source_type="official_registry_bulk",
            retrieved_at="2026-10-02T10:00:00Z",
            authority=1.0,
            evidence_span="Aa-registeret registered employee count: 5",
        )
        v_web = SourceFactVariant(
            value=8,
            source_url="https://company.example.com",
            source_type="company_owned",
            retrieved_at="2026-10-02T10:00:00Z",
            authority=0.85,
            evidence_span="Our company has 8 employees",
        )
        history = resolve_temporal_fact("employees", [v_statutory, v_web])
        self.assertEqual(history.current_value, 5)
        self.assertEqual(len(history.detected_changes), 0)
        web_hist = next(h for h in history.history if h["source_type"] == "company_owned")
        self.assertEqual(web_hist["temporal_status"], "conflicting_current")

    # 10. Wrong-company evidence must never enter history
    def test_wrong_company_evidence_must_never_enter_history(self):
        v_wrong = SourceFactVariant(
            value=999,
            source_url="https://external.example.com/competitor",
            source_type="job_board",
            retrieved_at="2026-10-02T10:00:00Z",
            entity_match_state="rejected",
            publishable=False,
            confidence=0.1,
            evidence_span="Competitor has 999 jobs",
        )
        v_correct = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            source_type="official_registry_bulk",
            retrieved_at="2026-10-02T10:00:00Z",
            entity_match_state="verified_match",
            publishable=True,
            confidence=1.0,
        )
        history = resolve_temporal_fact("employees", [v_wrong, v_correct])
        self.assertEqual(history.current_value, 5)
        # Wrong-company candidate is 100% excluded from history
        self.assertEqual(len(history.history), 1)
        self.assertEqual(history.history[0]["value"], 5)
        self.assertNotIn(999, [h["value"] for h in history.history])

    # 11. Ambiguous entity must not enter current profile
    def test_ambiguous_entity_must_not_enter_current_profile(self):
        v_ambiguous = SourceFactVariant(
            value="Speculative Name Claim",
            source_url="https://blog.example.com/news",
            source_type="permitted_public_page",
            retrieved_at="2026-10-02T10:00:00Z",
            entity_match_state="ambiguous",
            publishable=False,
            confidence=0.5,
        )
        history = resolve_temporal_fact("legal_name", [v_ambiguous])
        self.assertIsNone(history.current_value)
        self.assertEqual(len(history.detected_changes), 0)

    # 12. Superseded fact remains in provenance
    def test_superseded_fact_remains_in_provenance(self):
        v_old = SourceFactVariant(
            value="Old Purpose",
            source_url="https://brreg.no/archive/2018",
            source_type="official_registry_live",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2018-01-01",
            authority=1.0,
            evidence_span="Purpose was mining",
        )
        v_new = SourceFactVariant(
            value="New Purpose",
            source_url="https://brreg.no/current",
            source_type="official_registry_live",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date="2024-01-01",
            authority=1.0,
            evidence_span="Purpose is consulting",
        )
        history = resolve_temporal_fact("purpose", [v_old, v_new])
        self.assertEqual(history.current_value, "New Purpose")
        self.assertEqual(len(history.history), 2)
        old_hist = history.history[0]
        self.assertEqual(old_hist["value"], "Old Purpose")
        self.assertEqual(old_hist["source_url"], "https://brreg.no/archive/2018")
        self.assertEqual(old_hist["effective_date"], "2018-01-01")
        self.assertEqual(old_hist["evidence_span"], "Purpose was mining")

    # 13. Deterministic ordering of history
    def test_deterministic_ordering_of_history(self):
        v1 = SourceFactVariant(value=10, source_url="https://url1", source_type="official_financials", retrieved_at="2026-10-02T10:00:00Z", effective_date="2021-12-31")
        v2 = SourceFactVariant(value=20, source_url="https://url2", source_type="official_financials", retrieved_at="2026-10-02T10:00:00Z", effective_date="2022-12-31")
        v3 = SourceFactVariant(value=30, source_url="https://url3", source_type="official_financials", retrieved_at="2026-10-02T10:00:00Z", effective_date="2023-12-31")

        h_forward = resolve_temporal_fact("test", [v1, v2, v3])
        h_reverse = resolve_temporal_fact("test", [v3, v2, v1])
        h_shuffled = resolve_temporal_fact("test", [v2, v1, v3])

        self.assertEqual([h["value"] for h in h_forward.history], [10, 20, 30])
        self.assertEqual([h["value"] for h in h_reverse.history], [10, 20, 30])
        self.assertEqual([h["value"] for h in h_shuffled.history], [10, 20, 30])

    # 14. No fabricated dates
    def test_no_fabricated_dates(self):
        v = SourceFactVariant(
            value="Test Value",
            source_url="https://example.com/test",
            source_type="company_owned",
            retrieved_at="2026-10-02T10:00:00Z",
            effective_date=None,
            publication_date=None,
            authority=0.85,
        )
        history = resolve_temporal_fact("test_field", [v])
        hist_rec = history.history[0]
        self.assertIsNone(hist_rec["effective_date"])
        self.assertIsNone(hist_rec["publication_date"])

    # 15. Complete Profile Temporal Synthesis
    def test_build_company_temporal_profile(self):
        profile = {
            "organisation_number": "916340257",
            "name": "WYSSEN NORGE AS",
            "employees": 5,
            "evidence": {
                "registry": {
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "value": {
                        "registreringsdatoEnhetsregisteret": "2015-11-20",
                    },
                },
                "financials": {
                    "source_url": "https://data.brreg.no/regnskap",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "value": {
                        "records": [
                            {
                                "period": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
                                "revenue": 12000000,
                                "annual_result": 500000,
                            },
                            {
                                "period": {"fraDato": "2022-01-01", "tilDato": "2022-12-31"},
                                "revenue": 9500000,
                                "annual_result": -200000,
                            },
                        ]
                    },
                },
                "roles": {
                    "source_url": "https://data.brreg.no/roller",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "value": {
                        "roles": [
                            {"name": "Christian Wyssen", "role_code": "LEDE", "last_changed": "2020-05-15", "inactive": False},
                            {"name": "Eirik Næss", "role_code": "DAGL", "last_changed": "2023-08-01", "inactive": False},
                        ]
                    },
                },
                "workforce": {
                    "value": {
                        "conflicts": [
                            {
                                "field_name": "employees",
                                "all_variants": [
                                    {
                                        "value": 8,
                                        "source_url": "https://wyssenavalanche.com",
                                        "source_type": "company_owned",
                                        "retrieved_at": "2026-10-02T10:00:00Z",
                                        "authority": 0.85,
                                        "evidence_span": "8 specialists",
                                        "publishable": True,
                                        "entity_match_state": "verified_match",
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        }

        t_prof = build_company_temporal_profile(profile)
        self.assertEqual(t_prof.organisation_number, "916340257")
        self.assertEqual(t_prof.current_facts["employees"]["value"], 5)
        self.assertEqual(t_prof.current_facts["revenue"]["value"], 12000000)
        self.assertEqual(t_prof.current_facts["ceo"]["value"], "Eirik Næss")

        # Revenue changed from 9,500,000 to 12,000,000
        rev_hist = t_prof.fact_histories["revenue"]
        self.assertEqual(len(rev_hist["detected_changes"]), 1)
        self.assertEqual(rev_hist["detected_changes"][0]["old_value"], 9500000)
        self.assertEqual(rev_hist["detected_changes"][0]["new_value"], 12000000)

        # Simultaneous workforce conflict: registry 5 vs web 8
        emp_hist = t_prof.fact_histories["employees"]
        self.assertEqual(len(emp_hist["detected_changes"]), 0)  # Correctly NOT classified as temporal change!
        self.assertEqual(len(emp_hist["history"]), 2)


if __name__ == "__main__":
    unittest.main()
