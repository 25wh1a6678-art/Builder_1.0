from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.entity_resolution import (
    EntityResolutionResult,
    resolve_candidate_entity,
)
from norway_company_agent.conflict_resolution import (
    ConflictResolutionRecord,
    SourceFactVariant,
    resolve_business_description_conflict,
    resolve_scalar_conflict,
)


class TestEntityAndConflictResolution(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "organisation_number": "916340257",
            "name": "WYSSEN NORGE AS",
            "legal_form": "AS",
            "municipality": "SOGNDAL",
            "business_address": "Campus Fosshaugane, Trolladalen 30",
            "postal_code": "6856",
            "postal_area": "SOGNDAL",
            "phone": "57670000",
            "website": "www.wyssen.no",
            "evidence": {
                "registry": {
                    "value": {
                        "forretningsadresse.adresse": "Campus Fosshaugane, Trolladalen 30",
                        "forretningsadresse.postnummer": "6856",
                        "forretningsadresse.poststed": "SOGNDAL",
                        "telefon": "57670000",
                    }
                },
                "website": {
                    "value": {"final_url": "https://www.wyssen.no/"}
                },
                "locations": {
                    "value": {
                        "locations": [
                            {"name": "WYSSEN NORGE AS AVD SOGNDAL", "organisasjonsnummer": "916537050"},
                            {"name": "WYSSEN NORGE AS AVD TROMSØ", "organisasjonsnummer": "930677248"},
                        ]
                    }
                },
            },
        }

    # 1. Exact organisation-number match
    def test_exact_organisation_number_match(self):
        candidate = {
            "name": "Wyssen Norge",
            "organisation_number": "916340257",
            "url": "https://example.com/company/916340257",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertEqual(res.match_state, "verified_match")
        self.assertTrue(res.publishable)
        self.assertGreaterEqual(res.confidence, 0.95)
        self.assertIn("exact_organisation_number_match", res.matched_signals)

    # 2. Domain match
    def test_domain_match(self):
        candidate = {
            "name": "Wyssen Norge",
            "url": "https://wyssen.no/contact-us",
            "text": "Contact our Norwegian engineering team.",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertIn(res.match_state, {"verified_match", "probable_match"})
        self.assertTrue(res.publishable)
        self.assertIn("verified_domain_match:wyssen.no", res.matched_signals)

    # 3. Address match
    def test_address_match(self):
        candidate = {
            "name": "Wyssen Norge AS",
            "address": "Campus Fosshaugane, Trolladalen 30, 6856 Sogndal",
            "url": "https://directory.no/item/123",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertEqual(res.match_state, "verified_match")
        self.assertTrue(res.publishable)
        self.assertIn("exact_street_address_match", res.matched_signals)
        self.assertIn("exact_postcode_match:6856", res.matched_signals)

    # 4. Name-only false positive
    def test_name_only_false_positive_rejected_or_ambiguous(self):
        # Candidate has the same company name, but no org number, no address, no phone, different domain
        candidate = {
            "name": "WYSSEN NORGE AS",
            "url": "https://unrelated-domain.com/random-page",
            "text": "Some text mentioning Wyssen Norge AS casually with no identifiers.",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertEqual(res.match_state, "ambiguous")
        self.assertFalse(res.publishable)
        self.assertIn("name_only_unsupported_by_address_phone_or_domain", res.rejected_signals)

    # 5. Same-name different company (different org number)
    def test_same_name_different_company(self):
        # Candidate has identical name but different org number (e.g. competitor or old bankrupt company)
        candidate = {
            "name": "WYSSEN NORGE AS",
            "organisation_number": "999888777",
            "address": "Storgata 1, Oslo",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertEqual(res.match_state, "rejected")
        self.assertFalse(res.publishable)
        self.assertEqual(res.confidence, 0.0)

    # 6. Company vs Subunit
    def test_company_vs_subunit_resolution(self):
        candidate = {
            "name": "WYSSEN NORGE AS AVD SOGNDAL",
            "organisation_number": "916537050",
            "address": "Sogndal",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertEqual(res.match_state, "verified_match")
        self.assertTrue(res.publishable)
        self.assertIn("candidate_org_is_registered_subunit:916537050", res.matched_signals)

    # 7. Parent vs Subsidiary
    def test_parent_vs_subsidiary(self):
        candidate = {
            "name": "Wyssen Avalanche Control Nordics",
            "parent_organisation_number": "916340257",
            "url": "https://wyssen.no/nordics",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertTrue(res.publishable)
        self.assertIn("candidate_declares_target_as_parent", res.matched_signals)

    # 8. Ambiguous candidate
    def test_ambiguous_candidate(self):
        candidate = {
            "name": "Wyssen Consulting",
            "url": "https://other-site.com",
            "text": "General advisory services",
        }
        res = resolve_candidate_entity(self.profile, candidate)
        self.assertIn(res.match_state, {"ambiguous", "rejected"})
        self.assertFalse(res.publishable)

    # 9. Conflicting employee counts
    def test_conflicting_employee_counts_authority_resolution(self):
        v1 = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            source_type="official_registry_bulk",
            retrieved_at="2026-09-14T00:00:00Z",
            authority=1.0,
            confidence=1.0,
        )
        v2 = SourceFactVariant(
            value=12,
            source_url="https://wyssen.no/about",
            source_type="company_owned",
            retrieved_at="2026-09-10T00:00:00Z",
            authority=0.85,
            confidence=0.9,
        )
        rec = resolve_scalar_conflict("employees", [v1, v2])
        self.assertTrue(rec.has_conflict)
        # Official Aa-registeret takes precedence
        self.assertEqual(rec.selected_value, 5)
        self.assertEqual(rec.resolution_strategy, "source_authority_precedence")
        self.assertEqual(len(rec.all_variants), 2)  # All sources preserved

    # 10. Conflicting website/business descriptions
    def test_conflicting_business_descriptions(self):
        statutory = "Produksjon og salg av snøskredvarsling."
        web_desc = "Wyssen Norge tilbyr mobile taubaner og skredsikring i Norge."
        rec = resolve_business_description_conflict(statutory, web_desc)
        self.assertIn("Commercial description", rec.selected_value)
        self.assertIn("Statutory purpose", rec.selected_value)
        self.assertEqual(len(rec.all_variants), 2)
        self.assertEqual(rec.resolution_strategy, "multi_view_harmonization")

    # 11. Stale vs Current source
    def test_stale_vs_current_source_currency(self):
        v_old = SourceFactVariant(
            value="Old Address 1",
            source_url="https://brreg.no/api/v1",
            source_type="official_registry_live",
            retrieved_at="2023-01-01T00:00:00Z",
            effective_at="2023-01-01T00:00:00Z",
            authority=1.0,
        )
        v_new = SourceFactVariant(
            value="Campus Fosshaugane 30",
            source_url="https://brreg.no/api/v2",
            source_type="official_registry_live",
            retrieved_at="2026-09-15T00:00:00Z",
            effective_at="2026-09-15T00:00:00Z",
            authority=1.0,
        )
        rec = resolve_scalar_conflict("address", [v_old, v_new])
        self.assertTrue(rec.has_conflict)
        self.assertEqual(rec.selected_value, "Campus Fosshaugane 30")
        self.assertEqual(rec.resolution_strategy, "temporal_currency_precedence")
        self.assertEqual(len(rec.all_variants), 2)

    # 12. Rejected wrong-company evidence
    def test_rejected_wrong_company_evidence(self):
        wrong_candidate = {
            "name": "NORDIC DRILLING ASA",
            "organisation_number": "988777666",
            "address": "Stavanger",
        }
        res = resolve_candidate_entity(self.profile, wrong_candidate)
        self.assertEqual(res.match_state, "rejected")
        self.assertFalse(res.publishable)
        self.assertIn("conflicting_organisation_number", res.rejected_signals[0])
