from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.conflict_resolution import SourceFactVariant
from norway_company_agent.evidence_quality import (
    ClaimSpanValidationResult,
    SourceQualityAssessment,
    assess_source_quality,
    calibrate_fact_confidence,
    compute_content_sha256,
    validate_claim_span,
)
from norway_company_agent.temporal import resolve_temporal_fact


class TestEvidenceQualityAndGrounding(unittest.TestCase):

    # 1. Exact evidence span
    def test_exact_evidence_span(self):
        content = "Wyssen Norge AS is a specialist in avalanche mitigation systems."
        span = "avalanche mitigation systems"
        res = validate_claim_span(content, span)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.match_type, "exact")
        self.assertEqual(res.occurrence_count, 1)
        self.assertIsNotNone(res.content_sha256)

    # 2. Missing evidence span
    def test_missing_evidence_span(self):
        content = "Wyssen Norge AS provides avalanche towers in Norway."
        span = "industrial chemical processing plants"
        res = validate_claim_span(content, span)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.match_type, "missing")
        self.assertIn("does not exist", res.explanation)

    # 3. Wrong-company evidence hard gate
    def test_wrong_company_evidence_hard_gate(self):
        sq = assess_source_quality(
            source_url="https://external.example.com",
            source_type="job_board",
            has_evidence_span=True,
            is_span_validated=True,
        )
        cal = calibrate_fact_confidence(
            source_quality=sq,
            entity_match_state="rejected",
            entity_confidence=0.1,
        )
        self.assertEqual(cal.confidence, 0.0)
        self.assertFalse(cal.publishable)
        self.assertIn("rejected", cal.explanation)

    # 4. Ambiguous entity evidence
    def test_ambiguous_entity_evidence(self):
        sq = assess_source_quality(
            source_url="https://external.example.com",
            source_type="permitted_public_page",
            has_evidence_span=True,
            is_span_validated=True,
        )
        cal = calibrate_fact_confidence(
            source_quality=sq,
            entity_match_state="ambiguous",
            entity_confidence=0.6,
        )
        self.assertLessEqual(cal.confidence, 0.40)
        self.assertFalse(cal.publishable)
        self.assertIn("ambiguous", cal.explanation)

    # 5. Official source vs secondary source
    def test_official_source_vs_secondary_source(self):
        sq_official = assess_source_quality(
            source_url="https://data.brreg.no/api",
            source_type="official_registry_bulk",
            has_evidence_span=True,
            is_span_validated=True,
        )
        sq_secondary = assess_source_quality(
            source_url="https://directory.no/item",
            source_type="company_directory",
            has_evidence_span=True,
            is_span_validated=True,
        )
        self.assertEqual(sq_official.authority, 1.0)
        self.assertEqual(sq_official.directness, "statutory_primary")
        self.assertEqual(sq_secondary.authority, 0.65)
        self.assertEqual(sq_secondary.directness, "secondary_aggregator")
        self.assertGreater(sq_official.quality_score, sq_secondary.quality_score)

    # 6. Stale source vs fresh source
    def test_stale_source_vs_fresh_source(self):
        sq_fresh = assess_source_quality(
            source_url="https://company.no",
            source_type="company_owned",
            effective_date="2025-06-01",
            has_evidence_span=True,
            is_span_validated=True,
        )
        sq_stale = assess_source_quality(
            source_url="https://company.no",
            source_type="company_owned",
            effective_date="2018-01-01",
            has_evidence_span=True,
            is_span_validated=True,
        )
        cal_fresh = calibrate_fact_confidence(sq_fresh, "verified_match")
        cal_stale = calibrate_fact_confidence(sq_stale, "verified_match")
        self.assertEqual(sq_fresh.freshness, "fresh")
        self.assertEqual(sq_stale.freshness, "stale")
        self.assertGreater(cal_fresh.confidence, cal_stale.confidence)

    # 7. Conflicting sources
    def test_conflicting_sources(self):
        sq = assess_source_quality(
            source_url="https://example.no",
            source_type="job_board",
            has_evidence_span=True,
        )
        cal_unresolved = calibrate_fact_confidence(
            sq, "verified_match", is_unresolved_conflict=True
        )
        self.assertFalse(cal_unresolved.publishable)
        self.assertLessEqual(cal_unresolved.confidence, 0.45)

    # 8. Corroborating sources bonus
    def test_corroborating_sources(self):
        sq = assess_source_quality(
            source_url="https://example.no",
            source_type="company_owned",
            has_evidence_span=True,
            is_span_validated=True,
        )
        cal_single = calibrate_fact_confidence(sq, "verified_match", corroborating_sources_count=1)
        cal_corroborated = calibrate_fact_confidence(sq, "verified_match", corroborating_sources_count=2)
        self.assertGreater(cal_corroborated.confidence, cal_single.confidence)
        self.assertEqual(cal_corroborated.corroboration_bonus, 0.05)

    # 9. Low-confidence claim rejection
    def test_low_confidence_claim_rejection(self):
        # Secondary directory with stale data and no validated span
        sq_weak = assess_source_quality(
            source_url="https://directory.no/weak",
            source_type="company_directory",
            effective_date="2017-01-01",
            has_evidence_span=False,
            is_span_validated=False,
        )
        cal_weak = calibrate_fact_confidence(sq_weak, "probable_match")
        self.assertFalse(cal_weak.publishable)
        self.assertLess(cal_weak.confidence, 0.70)

    # 10. Deterministic confidence calculation
    def test_deterministic_confidence_calculation(self):
        sq = assess_source_quality(
            source_url="https://data.brreg.no",
            source_type="official_registry_bulk",
            has_evidence_span=True,
            is_span_validated=True,
        )
        c1 = calibrate_fact_confidence(sq, "verified_match")
        c2 = calibrate_fact_confidence(sq, "verified_match")
        self.assertEqual(c1.confidence, c2.confidence)
        self.assertEqual(c1.publishable, c2.publishable)
        self.assertEqual(c1.explanation, c2.explanation)

    # 11. Hash stability
    def test_hash_stability(self):
        payload = "<html><body><h1>Wyssen Norge AS</h1><p>Org.nr: 916340257</p></body></html>"
        h1 = compute_content_sha256(payload)
        h2 = compute_content_sha256(payload)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    # 12. Changed-content hash
    def test_changed_content_hash(self):
        p1 = "Revenue 10 000 000 NOK"
        p2 = "Revenue 12 000 000 NOK"
        h1 = compute_content_sha256(p1)
        h2 = compute_content_sha256(p2)
        self.assertNotEqual(h1, h2)

    # 13. Unicode evidence
    def test_unicode_evidence(self):
        content = "Selskapet er lokalisert i Tromsø og Sogndal med vedtektsfestet formål."
        span = "Tromsø og Sogndal"
        res = validate_claim_span(content, span)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.match_type, "exact")

    # 14. Numeric evidence
    def test_numeric_evidence(self):
        content = "Årsresultat for 2024 utgjorde 4 678 118 NOK mot fjorårets 5 161 209 NOK."
        span = "4 678 118 NOK"
        res = validate_claim_span(content, span)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.match_type, "exact")

    # 15. Multiple matching spans
    def test_multiple_matching_spans(self):
        content = "Wyssen Norge AS operates in Norway. Wyssen Norge AS was founded in 2015."
        span = "Wyssen Norge AS"
        res = validate_claim_span(content, span)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.occurrence_count, 2)

    # 16. Provenance preservation
    def test_provenance_preservation(self):
        v1 = SourceFactVariant(
            value=5,
            source_url="https://data.brreg.no/registry",
            source_type="official_registry_bulk",
            retrieved_at="2026-10-02T10:00:00Z",
            authority=1.0,
            evidence_span="5 employees",
        )
        v2 = SourceFactVariant(
            value=8,
            source_url="https://company.no/about",
            source_type="company_owned",
            retrieved_at="2026-10-02T10:00:00Z",
            authority=0.85,
            evidence_span="8 employees",
        )
        history = resolve_temporal_fact("employees", [v1, v2])
        self.assertEqual(history.current_value, 5)
        self.assertEqual(history.selected_value_info["selected_value"], 5)
        self.assertEqual(history.selected_value_info["source"], "https://data.brreg.no/registry")
        self.assertEqual(len(history.rejected_alternatives), 1)
        self.assertEqual(history.rejected_alternatives[0]["alternative_value"], 8)
        self.assertEqual(history.rejected_alternatives[0]["source"], "https://company.no/about")

    # 17. Rejected evidence preservation
    def test_rejected_evidence_preservation(self):
        v_correct = SourceFactVariant(
            value="WYSSEN NORGE AS",
            source_url="https://data.brreg.no",
            source_type="official_registry_bulk",
            retrieved_at="2026-10-02T10:00:00Z",
            authority=1.0,
            evidence_span="WYSSEN NORGE AS",
        )
        v_wrong = SourceFactVariant(
            value="WRONG COMPANY AS",
            source_url="https://spam.example.com",
            source_type="secondary_source",
            retrieved_at="2026-10-02T10:00:00Z",
            entity_match_state="rejected",
            publishable=False,
            authority=0.4,
        )
        history = resolve_temporal_fact("name", [v_correct, v_wrong])
        self.assertEqual(history.current_value, "WYSSEN NORGE AS")
        # Rejected candidate preserved in rejected_alternatives with clear rejection reason
        self.assertEqual(len(history.rejected_alternatives), 1)
        self.assertEqual(history.rejected_alternatives[0]["alternative_value"], "WRONG COMPANY AS")
        self.assertEqual(history.rejected_alternatives[0]["entity_match"], "rejected")

    # 18. Case and whitespace normalization
    def test_case_and_whitespace_normalization(self):
        content = "Wyssen   Norge\n\tAS   operating in\nNorway."
        span = "Wyssen Norge AS operating in Norway."
        res = validate_claim_span(content, span)
        self.assertTrue(res.is_valid)
        self.assertIn(res.match_type, {"normalized_whitespace", "case_insensitive"})


if __name__ == "__main__":
    unittest.main()
