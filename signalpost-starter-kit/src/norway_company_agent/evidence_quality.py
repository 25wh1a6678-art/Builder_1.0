from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Literal
import unicodedata

from .conflict_resolution import SOURCE_AUTHORITY_RANK
from .evidence import utc_now


SourceCategory = Literal[
    "statutory_registry",
    "official_company_website",
    "official_company_career_page",
    "public_job_board",
    "review_platform",
    "search_index_result",
    "secondary_source",
]

SourceDirectness = Literal[
    "statutory_primary",
    "first_party",
    "secondary_aggregator",
    "third_party_unverified",
]

EvidenceCompleteness = Literal[
    "verified_exact_span",
    "partial_span",
    "unverified_span",
    "no_span",
]

FreshnessState = Literal[
    "fresh",
    "stale",
    "historical",
    "unanchored",
]


@dataclass
class ClaimSpanValidationResult:
    is_valid: bool
    match_type: str  # "exact", "normalized_whitespace", "case_insensitive", "html_extracted", "missing"
    matched_span: str | None
    occurrence_count: int
    content_sha256: str | None
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceQualityAssessment:
    source_url: str
    source_type: str
    category: SourceCategory
    directness: SourceDirectness
    freshness: FreshnessState
    authority: float
    entity_confidence: float
    evidence_completeness: EvidenceCompleteness
    quality_score: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalibratedConfidenceResult:
    confidence: float
    publishable: bool
    base_authority: float
    entity_factor: float
    freshness_factor: float
    completeness_modifier: float
    corroboration_bonus: float
    conflict_adjustment: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GroundedClaim:
    claim_field: str
    value: Any
    source_url: str
    source_type: str
    source_category: SourceCategory
    source_directness: SourceDirectness
    source_freshness: FreshnessState
    retrieved_at: str
    publication_date: str | None = None
    effective_date: str | None = None
    organisation_number: str | None = None
    entity_match_state: str = "verified_match"
    entity_match_confidence: float = 1.0
    authority: float = 0.7
    confidence: float = 1.0
    publishable: bool = True
    claim_span: str = ""
    span_validation: dict[str, Any] = field(default_factory=dict)
    content_sha256: str | None = None
    selection_reason: str = ""
    rejection_reason: str | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_content_sha256(content: str | bytes | None) -> str | None:
    """Calculate deterministic SHA-256 over exact retrieved payload or text."""
    if content is None:
        return None
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_text_for_search(text: str) -> str:
    """Normalize unicode and collapse whitespace."""
    nfkc = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", nfkc).strip()


def _strip_html(text: str) -> str:
    """Strip basic HTML tags while preserving content."""
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean).strip()


def validate_claim_span(
    content: str | bytes | None,
    claim_span: str | None,
) -> ClaimSpanValidationResult:
    """Validate that the claimed evidence span actually exists in the retrieved source content."""
    if not claim_span or not claim_span.strip():
        return ClaimSpanValidationResult(
            is_valid=False,
            match_type="missing",
            matched_span=None,
            occurrence_count=0,
            content_sha256=compute_content_sha256(content),
            explanation="Claim span is empty or absent.",
        )

    if content is None:
        return ClaimSpanValidationResult(
            is_valid=False,
            match_type="missing",
            matched_span=None,
            occurrence_count=0,
            content_sha256=None,
            explanation="Source content payload is absent; evidence span cannot be verified.",
        )

    content_str = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    span_str = str(claim_span).strip()
    c_hash = compute_content_sha256(content)

    # 1. Exact verbatim match
    if span_str in content_str:
        occ = content_str.count(span_str)
        return ClaimSpanValidationResult(
            is_valid=True,
            match_type="exact",
            matched_span=span_str,
            occurrence_count=occ,
            content_sha256=c_hash,
            explanation=f"Exact verbatim span found ({occ} occurrence(s)) in source payload.",
        )

    # 2. Whitespace-normalized match
    norm_content = _normalize_text_for_search(content_str)
    norm_span = _normalize_text_for_search(span_str)
    if norm_span in norm_content:
        occ = norm_content.count(norm_span)
        return ClaimSpanValidationResult(
            is_valid=True,
            match_type="normalized_whitespace",
            matched_span=norm_span,
            occurrence_count=occ,
            content_sha256=c_hash,
            explanation=f"Span found with whitespace normalization ({occ} occurrence(s)).",
        )

    # 3. Case-insensitive match
    lower_content = norm_content.casefold()
    lower_span = norm_span.casefold()
    if lower_span in lower_content:
        occ = lower_content.count(lower_span)
        return ClaimSpanValidationResult(
            is_valid=True,
            match_type="case_insensitive",
            matched_span=norm_span,
            occurrence_count=occ,
            content_sha256=c_hash,
            explanation=f"Span found with case-insensitive matching ({occ} occurrence(s)).",
        )

    # 4. HTML tag stripped match (for raw HTML payloads)
    if "<" in content_str and ">" in content_str:
        stripped_content = _strip_html(content_str)
        if norm_span in stripped_content or lower_span in stripped_content.casefold():
            occ = stripped_content.casefold().count(lower_span)
            return ClaimSpanValidationResult(
                is_valid=True,
                match_type="html_extracted",
                matched_span=norm_span,
                occurrence_count=occ,
                content_sha256=c_hash,
                explanation=f"Span found in HTML-extracted text ({occ} occurrence(s)).",
            )

    return ClaimSpanValidationResult(
        is_valid=False,
        match_type="missing",
        matched_span=None,
        occurrence_count=0,
        content_sha256=c_hash,
        explanation=f"Evidence span '{span_str[:60]}...' does not exist in retrieved source payload.",
    )


def classify_source_category(source_type: str) -> tuple[SourceCategory, SourceDirectness]:
    """Map source type to structured category and directness level."""
    st = str(source_type).casefold()
    if any(k in st for k in ("official", "registry", "brreg", "regnskap", "subunits", "roles")):
        return "statutory_registry", "statutory_primary"
    if any(k in st for k in ("career", "job_postings")):
        return "official_company_career_page", "first_party"
    if any(k in st for k in ("company_owned", "company_site", "website")):
        return "official_company_website", "first_party"
    if any(k in st for k in ("job_board", "nav", "arbeidsplassen", "stillinger")):
        return "public_job_board", "secondary_aggregator"
    if any(k in st for k in ("review", "rating", "places", "google")):
        return "review_platform", "secondary_aggregator"
    if any(k in st for k in ("search", "index", "bing", "serp")):
        return "search_index_result", "secondary_aggregator"
    if any(k in st for k in ("directory", "aggregator", "catalog")):
        return "secondary_source", "secondary_aggregator"
    return "secondary_source", "third_party_unverified"


def assess_source_freshness(
    effective_date: str | None = None,
    publication_date: str | None = None,
    retrieved_at: str | None = None,
) -> FreshnessState:
    """Assess temporal freshness of the source without inventing dates."""
    date_anchor = effective_date or publication_date
    if not date_anchor:
        return "unanchored"
    try:
        year_str = str(date_anchor)[:4]
        if year_str.isdigit():
            year = int(year_str)
            # Benchmark relative to 2026 challenge baseline
            if year >= 2024:
                return "fresh"
            elif year >= 2020:
                return "historical"
            else:
                return "stale"
    except Exception:
        pass
    return "unanchored"


def assess_source_quality(
    source_url: str,
    source_type: str,
    retrieved_at: str | None = None,
    effective_date: str | None = None,
    publication_date: str | None = None,
    entity_confidence: float = 1.0,
    has_evidence_span: bool = False,
    is_span_validated: bool = False,
) -> SourceQualityAssessment:
    """Produce structured assessment of source authority, directness, freshness, and completeness."""
    category, directness = classify_source_category(source_type)
    authority = SOURCE_AUTHORITY_RANK.get(source_type, 0.65)
    freshness = assess_source_freshness(effective_date, publication_date, retrieved_at)

    if is_span_validated:
        completeness = "verified_exact_span"
    elif has_evidence_span:
        completeness = "unverified_span"
    else:
        completeness = "no_span"

    # Directness weighting
    directness_weight = {
        "statutory_primary": 1.0,
        "first_party": 0.90,
        "secondary_aggregator": 0.75,
        "third_party_unverified": 0.50,
    }.get(directness, 0.70)

    # Freshness weighting
    freshness_weight = {
        "fresh": 1.0,
        "historical": 0.95,
        "unanchored": 0.90,
        "stale": 0.70,
    }.get(freshness, 0.90)

    # Completeness weighting
    comp_weight = {
        "verified_exact_span": 1.0,
        "partial_span": 0.95,
        "unverified_span": 0.85,
        "no_span": 0.40,
    }.get(completeness, 0.50)

    quality_score = round(authority * directness_weight * freshness_weight * comp_weight, 4)

    return SourceQualityAssessment(
        source_url=source_url,
        source_type=source_type,
        category=category,
        directness=directness,
        freshness=freshness,
        authority=authority,
        entity_confidence=entity_confidence,
        evidence_completeness=completeness,
        quality_score=quality_score,
        explanation=(
            f"Category '{category}' ({directness}) with base authority {authority:.2f}. "
            f"Freshness: {freshness}. Evidence: {completeness}. Quality score: {quality_score:.2f}."
        ),
    )


def calibrate_fact_confidence(
    source_quality: SourceQualityAssessment,
    entity_match_state: str,
    entity_confidence: float = 1.0,
    has_conflict: bool = False,
    is_unresolved_conflict: bool = False,
    corroborating_sources_count: int = 1,
) -> CalibratedConfidenceResult:
    """Deterministically calibrate confidence using hard precision rules and explainable formulas."""
    # Hard Rule 1: Rejected entity -> confidence 0, never publish
    if entity_match_state == "rejected" or entity_confidence < 0.50:
        return CalibratedConfidenceResult(
            confidence=0.0,
            publishable=False,
            base_authority=source_quality.authority,
            entity_factor=0.0,
            freshness_factor=1.0,
            completeness_modifier=0.0,
            corroboration_bonus=0.0,
            conflict_adjustment=0.0,
            explanation="Hard precision gate: entity resolution rejected or mismatched candidate company.",
        )

    # Hard Rule 2: Ambiguous entity -> confidence <= 0.40, never publish as verified fact
    if entity_match_state == "ambiguous":
        capped = min(0.40, round(entity_confidence * 0.5, 4))
        return CalibratedConfidenceResult(
            confidence=capped,
            publishable=False,
            base_authority=source_quality.authority,
            entity_factor=0.40,
            freshness_factor=1.0,
            completeness_modifier=0.0,
            corroboration_bonus=0.0,
            conflict_adjustment=0.0,
            explanation="Hard precision gate: entity identity is ambiguous. Quarantined from verified profile.",
        )

    # Hard Rule 3: Missing evidence span -> penalty, unverified
    if source_quality.evidence_completeness == "no_span":
        capped = min(0.40, round(source_quality.authority * 0.45, 4))
        return CalibratedConfidenceResult(
            confidence=capped,
            publishable=False,
            base_authority=source_quality.authority,
            entity_factor=1.0,
            freshness_factor=1.0,
            completeness_modifier=-0.40,
            corroboration_bonus=0.0,
            conflict_adjustment=0.0,
            explanation="Evidence gate: claim lacks verifiable evidence span. Not publishable.",
        )

    # Hard Rule 4: Unresolved conflict -> do not silently choose a value
    if is_unresolved_conflict:
        capped = min(0.45, round(source_quality.authority * 0.5, 4))
        return CalibratedConfidenceResult(
            confidence=capped,
            publishable=False,
            base_authority=source_quality.authority,
            entity_factor=1.0,
            freshness_factor=1.0,
            completeness_modifier=0.0,
            corroboration_bonus=0.0,
            conflict_adjustment=-0.35,
            explanation="Contemporaneous conflict: independent sources conflict with equal authority. Value withheld.",
        )

    # Deterministic Formula for Admissible Facts
    base_auth = source_quality.authority
    entity_factor = 1.0 if entity_match_state == "verified_match" else 0.88

    freshness_factor = {
        "fresh": 1.0,
        "historical": 0.95,
        "unanchored": 0.90,
        "stale": 0.70,
    }.get(source_quality.freshness, 0.90)

    completeness_modifier = {
        "verified_exact_span": 0.05,
        "partial_span": 0.0,
        "unverified_span": -0.08,
        "no_span": -0.40,
    }.get(source_quality.evidence_completeness, 0.0)

    # Corroboration bonus: 2+ independent agreeing sources yield +0.05 bonus
    corroboration_bonus = 0.05 if corroborating_sources_count >= 2 else 0.0
    conflict_adjustment = -0.10 if has_conflict else 0.0

    raw_score = (base_auth * entity_factor * freshness_factor) + completeness_modifier + corroboration_bonus + conflict_adjustment
    final_confidence = max(0.0, min(1.0, round(raw_score, 4)))

    # Only publish if confidence meets the precision threshold (>= 0.70) and entity is verified/probable
    publishable = (final_confidence >= 0.70) and (entity_match_state in {"verified_match", "probable_match"})

    explanation = (
        f"Calibrated confidence {final_confidence:.2f} (base: {base_auth:.2f}, "
        f"entity: {entity_factor:.2f}, freshness: {freshness_factor:.2f}, "
        f"span: {completeness_modifier:+.2f}, corroboration: {corroboration_bonus:+.2f}, "
        f"conflict: {conflict_adjustment:+.2f}). Publishable: {publishable}."
    )

    return CalibratedConfidenceResult(
        confidence=final_confidence,
        publishable=publishable,
        base_authority=base_auth,
        entity_factor=entity_factor,
        freshness_factor=freshness_factor,
        completeness_modifier=completeness_modifier,
        corroboration_bonus=corroboration_bonus,
        conflict_adjustment=conflict_adjustment,
        explanation=explanation,
    )
