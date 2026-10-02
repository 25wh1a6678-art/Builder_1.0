from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


SOURCE_AUTHORITY_RANK = {
    "official_registry_bulk": 1.0,
    "official_subunits": 1.0,
    "official_registry_live": 1.0,
    "official_financials": 1.0,
    "official_roles": 1.0,
    "official_api": 1.0,
    "company_owned": 0.85,
    "company_site": 0.85,
    "registry_linked_company_website": 0.85,
    "customer_review": 0.70,
    "permitted_public_page": 0.70,
    "company_directory": 0.65,
    "job_board": 0.60,
    "experimental": 0.40,
}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@dataclass
class SourceFactVariant:
    value: Any
    source_url: str
    source_type: str
    retrieved_at: str
    effective_at: str | None = None
    confidence: float = 1.0
    authority: float = 0.7
    evidence_span: str = ""
    entity_match_confidence: float = 1.0
    publication_date: str | None = None
    effective_date: str | None = None
    source_freshness: str | None = None
    temporal_status: str = "unknown_temporal_status"
    observed_at: str | None = None
    entity_match_state: str = "verified_match"
    publishable: bool = True

    def __post_init__(self) -> None:
        if self.effective_date is None and self.effective_at is not None:
            self.effective_date = self.effective_at
        elif self.effective_at is None and self.effective_date is not None:
            self.effective_at = self.effective_date
        if self.observed_at is None and self.retrieved_at is not None:
            self.observed_at = self.retrieved_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConflictResolutionRecord:
    field_name: str
    has_conflict: bool
    selected_value: Any
    selected_source: dict[str, Any]
    resolution_strategy: str
    resolution_explanation: str
    all_variants: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_scalar_conflict(
    field_name: str,
    variants: list[SourceFactVariant],
) -> ConflictResolutionRecord:
    """Compare multiple source fact variants and select the best-supported value while preserving all source evidence."""
    if not variants:
        return ConflictResolutionRecord(
            field_name=field_name,
            has_conflict=False,
            selected_value=None,
            selected_source={},
            resolution_strategy="no_sources",
            resolution_explanation="No source variants provided for resolution.",
            all_variants=[],
        )

    all_dicts = [v.to_dict() for v in variants]
    distinct_values = {v.value for v in variants}

    # If all sources agree, there is no conflict
    if len(distinct_values) == 1:
        best = max(variants, key=lambda v: (v.authority, v.confidence, v.retrieved_at or ""))
        return ConflictResolutionRecord(
            field_name=field_name,
            has_conflict=False,
            selected_value=best.value,
            selected_source=best.to_dict(),
            resolution_strategy="unanimous_agreement",
            resolution_explanation=f"All {len(variants)} source(s) agree on value '{best.value}'.",
            all_variants=all_dicts,
        )

    # 1. Authority Precedence (e.g. Official Statutory Registry vs Web Marketing)
    authorities = {v.authority for v in variants}
    if len(authorities) > 1:
        highest_auth = max(authorities)
        top_candidates = [v for v in variants if v.authority == highest_auth]
        if len({v.value for v in top_candidates}) == 1:
            best = max(top_candidates, key=lambda v: (v.confidence, v.retrieved_at or ""))
            return ConflictResolutionRecord(
                field_name=field_name,
                has_conflict=True,
                selected_value=best.value,
                selected_source=best.to_dict(),
                resolution_strategy="source_authority_precedence",
                resolution_explanation=(
                    f"Selected value '{best.value}' from higher-authority source ({best.source_type}, authority: {best.authority}) "
                    f"over conflicting secondary source values: {[v.value for v in variants if v.value != best.value]}. "
                    f"All source variants preserved."
                ),
                all_variants=all_dicts,
            )

    # 2. Temporal Currency Precedence (e.g. 2026 vs 2024 filing)
    dated_variants = [v for v in variants if _parse_iso(v.effective_at or v.retrieved_at)]
    if len(dated_variants) >= 2:
        sorted_by_date = sorted(
            dated_variants,
            key=lambda v: _parse_iso(v.effective_at or v.retrieved_at) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        newest = sorted_by_date[0]
        older = sorted_by_date[1]
        dt_new = _parse_iso(newest.effective_at or newest.retrieved_at)
        dt_old = _parse_iso(older.effective_at or older.retrieved_at)
        if dt_new and dt_old and (dt_new - dt_old).total_seconds() > 86_400 and newest.authority >= older.authority:
            return ConflictResolutionRecord(
                field_name=field_name,
                has_conflict=True,
                selected_value=newest.value,
                selected_source=newest.to_dict(),
                resolution_strategy="temporal_currency_precedence",
                resolution_explanation=(
                    f"Selected current value '{newest.value}' dated {newest.effective_at or newest.retrieved_at} "
                    f"over historical value '{older.value}' dated {older.effective_at or older.retrieved_at}. "
                    f"Historical provenance retained."
                ),
                all_variants=all_dicts,
            )

    # 3. Entity Match Confidence Precedence
    highest_conf = max(v.confidence for v in variants)
    high_conf_vars = [v for v in variants if v.confidence == highest_conf]
    if len({v.value for v in high_conf_vars}) == 1:
        best = high_conf_vars[0]
        return ConflictResolutionRecord(
            field_name=field_name,
            has_conflict=True,
            selected_value=best.value,
            selected_source=best.to_dict(),
            resolution_strategy="entity_confidence_precedence",
            resolution_explanation=(
                f"Selected value '{best.value}' due to higher entity resolution confidence ({best.confidence}) "
                f"over lower-confidence candidate variants."
            ),
            all_variants=all_dicts,
        )

    # 4. Insoluble Conflict: Mark explicitly rather than guessing
    return ConflictResolutionRecord(
        field_name=field_name,
        has_conflict=True,
        selected_value=None,
        selected_source={},
        resolution_strategy="explicit_unresolved_conflict",
        resolution_explanation=(
            f"Unresolved conflict: {len(variants)} independent sources provide contradictory values "
            f"{list(distinct_values)} with equivalent authority and currency. Value not fabricated."
        ),
        all_variants=all_dicts,
    )


def resolve_business_description_conflict(
    statutory_purpose: str,
    website_description: str,
    statutory_url: str = "https://data.brreg.no/enhetsregisteret/api/enheter",
    website_url: str = "",
) -> ConflictResolutionRecord:
    """Synthesize multi-view description preserving statutory purpose and operational website description."""
    variants = []
    if statutory_purpose:
        variants.append(SourceFactVariant(
            value=statutory_purpose,
            source_url=statutory_url,
            source_type="official_registry_bulk",
            retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            authority=1.0,
            evidence_span=f"Vedtektsfestet formål: {statutory_purpose[:300]}",
        ))
    if website_description:
        variants.append(SourceFactVariant(
            value=website_description,
            source_url=website_url or "https://company.example.com",
            source_type="company_owned",
            retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            authority=0.85,
            evidence_span=f"Commercial website description: {website_description[:300]}",
        ))

    combined = []
    if website_description:
        combined.append(f"Commercial description: {website_description.strip()}")
    if statutory_purpose:
        combined.append(f"Statutory purpose: {statutory_purpose.strip()}")

    synthesized_text = " ".join(combined)
    return ConflictResolutionRecord(
        field_name="business_description",
        has_conflict=False,  # Harmonized through dual-layer synthesis
        selected_value=synthesized_text,
        selected_source=variants[0].to_dict() if variants else {},
        resolution_strategy="multi_view_harmonization",
        resolution_explanation=(
            "Harmonized statutory legal purpose from official registry with commercial operational description from company website. Both sources attributed."
        ),
        all_variants=[v.to_dict() for v in variants],
    )
