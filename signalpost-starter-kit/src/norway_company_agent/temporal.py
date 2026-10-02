from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal

from .conflict_resolution import (
    SOURCE_AUTHORITY_RANK,
    SourceFactVariant,
    _parse_iso,
)
from .evidence import utc_now
from .evidence_quality import (
    assess_source_quality,
    calibrate_fact_confidence,
    compute_content_sha256,
)


TemporalStatus = Literal[
    "current",
    "historical",
    "superseded",
    "conflicting_current",
    "unknown_temporal_status",
]


@dataclass
class ChangeEvent:
    field: str
    old_value: Any
    new_value: Any
    change_type: str  # "temporal_change", "status_update", "restatement"
    old_source: str
    new_source: str
    old_date: str | None = None
    new_date: str | None = None
    confidence: float = 1.0
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactHistory:
    field_name: str
    current_value: Any
    current_variant: dict[str, Any] | None
    temporal_status: str
    has_unresolved_conflict: bool
    history: list[dict[str, Any]] = field(default_factory=list)
    detected_changes: list[dict[str, Any]] = field(default_factory=list)
    unresolved_conflicts: list[dict[str, Any]] = field(default_factory=list)
    selected_value_info: dict[str, Any] = field(default_factory=dict)
    rejected_alternatives: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TemporalProfile:
    organisation_number: str
    current_facts: dict[str, Any] = field(default_factory=dict)
    fact_histories: dict[str, dict[str, Any]] = field(default_factory=dict)
    detected_changes: list[dict[str, Any]] = field(default_factory=list)
    unresolved_conflicts: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_date_anchor(date_str: str | None) -> datetime | None:
    """Parse date or year string into UTC datetime for deterministic temporal comparison."""
    if not date_str:
        return None
    raw = str(date_str).strip()
    # 1. Year only (e.g. '2023')
    if re.fullmatch(r"\d{4}", raw):
        return datetime(int(raw), 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    # 2. ISO timestamp or date
    dt = _parse_iso(raw)
    if dt:
        return dt
    # 3. YYYY-MM-DD
    try:
        parts = raw.split("-")
        if len(parts) == 3:
            return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def _get_variant_temporal_key(v: SourceFactVariant) -> tuple[datetime, int, float, float, str, str]:
    """Build deterministic tuple for chronological ordering.
    Tuple components:
    1. datetime anchor (oldest first)
    2. date precision priority (0=effective_date, 1=publication_date, 2=retrieved/observed, 3=none)
    3. negative authority (highest authority first if same date)
    4. negative confidence (highest confidence first)
    5. source_url (string tiebreaker)
    6. value string representation (deterministic tiebreaker)
    """
    dt_effective = _parse_date_anchor(v.effective_date)
    dt_pub = _parse_date_anchor(v.publication_date)
    dt_obs = _parse_date_anchor(v.observed_at or v.retrieved_at)

    if dt_effective is not None:
        anchor_dt = dt_effective
        precision = 0
    elif dt_pub is not None:
        anchor_dt = dt_pub
        precision = 1
    elif dt_obs is not None:
        anchor_dt = dt_obs
        precision = 2
    else:
        anchor_dt = datetime.min.replace(tzinfo=timezone.utc)
        precision = 3

    return (
        anchor_dt,
        precision,
        -float(v.authority),
        -float(v.confidence),
        str(v.source_url or ""),
        str(v.value or ""),
    )


def _extract_date_label(v: SourceFactVariant) -> str | None:
    """Return explicit date label without fabricating dates."""
    if v.effective_date:
        return str(v.effective_date)
    if v.publication_date:
        return str(v.publication_date)
    return None


def resolve_temporal_fact(
    field_name: str,
    variants: list[SourceFactVariant],
) -> FactHistory:
    """Deterministically track fact history, detect temporal changes, and resolve simultaneous conflicts."""
    # Step 1: Entity Resolution & Confidence Calibration Hard Gate
    admissible_variants: list[SourceFactVariant] = []
    quarantined_ambiguous: list[SourceFactVariant] = []
    rejected_alternatives: list[dict[str, Any]] = []

    for v in variants:
        sq = assess_source_quality(
            source_url=v.source_url,
            source_type=v.source_type,
            retrieved_at=v.retrieved_at,
            effective_date=v.effective_date,
            publication_date=v.publication_date,
            entity_confidence=getattr(v, "entity_match_confidence", 1.0),
            has_evidence_span=bool(v.evidence_span),
            is_span_validated=bool(getattr(v, "is_span_validated", False)),
        )
        cal = calibrate_fact_confidence(
            source_quality=sq,
            entity_match_state=getattr(v, "entity_match_state", "verified_match"),
            entity_confidence=getattr(v, "entity_match_confidence", 1.0),
            has_conflict=False,
        )

        # Check publishability and entity resolution match state
        is_rejected = (
            not getattr(v, "publishable", True)
            or getattr(v, "entity_match_state", "verified_match") == "rejected"
            or getattr(v, "entity_match_confidence", 1.0) < 0.50
            or not cal.publishable and getattr(v, "publishable", True) is False
        )
        is_ambiguous = getattr(v, "entity_match_state", "verified_match") == "ambiguous"

        if is_rejected:
            rejected_alternatives.append({
                "alternative_value": v.value,
                "source": v.source_url,
                "source_type": v.source_type,
                "rejection_reason": "Entity resolution rejected candidate company or low confidence.",
                "entity_match": getattr(v, "entity_match_state", "rejected"),
                "authority": v.authority,
                "temporal_status": "rejected",
            })
            continue
        elif is_ambiguous:
            rejected_alternatives.append({
                "alternative_value": v.value,
                "source": v.source_url,
                "source_type": v.source_type,
                "rejection_reason": "Entity identity is ambiguous; quarantined from verified profile.",
                "entity_match": "ambiguous",
                "authority": v.authority,
                "temporal_status": "ambiguous",
            })
            quarantined_ambiguous.append(v)
            continue

        v.confidence = cal.confidence
        admissible_variants.append(v)

    if not admissible_variants:
        return FactHistory(
            field_name=field_name,
            current_value=None,
            current_variant=None,
            temporal_status="unknown_temporal_status",
            has_unresolved_conflict=False,
            history=[],
            detected_changes=[],
            unresolved_conflicts=[],
            selected_value_info={},
            rejected_alternatives=rejected_alternatives,
        )

    # Step 2: Deterministic chronological sorting
    sorted_variants = sorted(admissible_variants, key=_get_variant_temporal_key)

    # Step 3: Group into distinct temporal buckets
    # A temporal bucket represents a discrete timeframe (e.g. specific effective_date, publication_date, or crawl window).
    buckets: list[list[SourceFactVariant]] = []
    current_bucket: list[SourceFactVariant] = []
    current_bucket_anchor: datetime | None = None
    current_bucket_has_explicit_date: bool = False

    for v in sorted_variants:
        v_dt, v_prec, _, _, _, _ = _get_variant_temporal_key(v)
        has_explicit = (v.effective_date is not None or v.publication_date is not None)

        if not current_bucket:
            current_bucket.append(v)
            current_bucket_anchor = v_dt
            current_bucket_has_explicit_date = has_explicit
            continue

        # Check if this variant belongs to the same temporal anchor
        is_same_anchor = False
        if has_explicit and current_bucket_has_explicit_date:
            # Both have explicit dates: compare date strings or daily timestamps
            v_label = _extract_date_label(v)
            b_label = _extract_date_label(current_bucket[0])
            is_same_anchor = (v_label == b_label)
        elif not has_explicit and not current_bucket_has_explicit_date:
            # Neither has explicit dates: if retrieved within 24 hours (86,400s), contemporaneous crawl window
            diff_sec = abs((v_dt - (current_bucket_anchor or v_dt)).total_seconds())
            is_same_anchor = (diff_sec <= 86400)
        else:
            # One has an explicit date (e.g. historical annual accounts) and the other is pure current crawl: separate!
            is_same_anchor = False

        if is_same_anchor:
            current_bucket.append(v)
        else:
            buckets.append(current_bucket)
            current_bucket = [v]
            current_bucket_anchor = v_dt
            current_bucket_has_explicit_date = has_explicit

    if current_bucket:
        buckets.append(current_bucket)

    # Step 4: Resolve each bucket (handling contemporaneous source conflicts)
    bucket_winners: list[SourceFactVariant] = []
    unresolved_conflicts: list[dict[str, Any]] = []

    for idx, b in enumerate(buckets):
        is_latest_bucket = (idx == len(buckets) - 1)
        distinct_vals = {item.value for item in b}

        if len(distinct_vals) == 1:
            # Unanimous agreement in this timeframe
            best = max(b, key=lambda item: (item.authority, item.confidence))
            bucket_winners.append(best)
        else:
            # Contemporaneous source conflict! (e.g. current registry 5 vs current website 8)
            # Apply V5 Authority Precedence
            max_auth = max(item.authority for item in b)
            top_auth_items = [item for item in b if item.authority == max_auth]
            distinct_top_vals = {item.value for item in top_auth_items}

            if len(distinct_top_vals) == 1:
                # Higher authority source resolves the conflict cleanly
                winner = max(top_auth_items, key=lambda item: item.confidence)
                for item in b:
                    if item.value != winner.value:
                        item.temporal_status = "conflicting_current" if is_latest_bucket else "superseded"
                bucket_winners.append(winner)
            else:
                # Equal authority conflict
                if is_latest_bucket:
                    # Insoluble conflict in current period: do not guess!
                    for item in b:
                        item.temporal_status = "conflicting_current"
                    unresolved_conflicts.append({
                        "field": field_name,
                        "conflicting_values": list(distinct_vals),
                        "sources": [item.to_dict() for item in b],
                        "explanation": f"Equal-authority contemporaneous conflict on '{field_name}': values {list(distinct_vals)}.",
                    })
                    bucket_winners.append(top_auth_items[0])  # provisional, but flag will be raised
                else:
                    winner = top_auth_items[0]
                    bucket_winners.append(winner)

    # Step 5: Detect genuine temporal changes across chronological buckets
    detected_changes: list[ChangeEvent] = []

    for i in range(len(bucket_winners) - 1):
        v_old = bucket_winners[i]
        v_new = bucket_winners[i + 1]

        # Older bucket variants are historical or superseded
        for v in buckets[i]:
            if v.temporal_status == "unknown_temporal_status":
                v.temporal_status = "historical"

        if v_old.value != v_new.value:
            # Values differ across distinct temporal anchors
            old_label = _extract_date_label(v_old) or str(v_old.observed_at or v_old.retrieved_at or "")[:10]
            new_label = _extract_date_label(v_new) or str(v_new.observed_at or v_new.retrieved_at or "")[:10]

            # Only emit change event if there is genuine temporal evidence
            detected_changes.append(ChangeEvent(
                field=field_name,
                old_value=v_old.value,
                new_value=v_new.value,
                change_type="temporal_change",
                old_source=v_old.source_url,
                new_source=v_new.source_url,
                old_date=old_label or None,
                new_date=new_label or None,
                confidence=min(float(v_old.confidence), float(v_new.confidence)),
                explanation=(
                    f"Temporal change in '{field_name}': shifted from '{v_old.value}' ({old_label or 'prior'}) "
                    f"to '{v_new.value}' ({new_label or 'current'})."
                ),
            ))
        else:
            # Value remained identical across dates: no change event
            pass

    # Step 6: Finalize current variant
    latest_bucket = buckets[-1]
    has_conflict = len(unresolved_conflicts) > 0
    winning_latest = bucket_winners[-1]
    selected_value_info: dict[str, Any] = {}

    if has_conflict:
        current_val = None
        current_var_dict = None
        final_temporal_status = "conflicting_current"
    else:
        current_val = winning_latest.value
        winning_latest.temporal_status = "current"
        current_var_dict = winning_latest.to_dict()
        final_temporal_status = "current"
        selected_value_info = {
            "selected_value": current_val,
            "confidence": winning_latest.confidence,
            "source": winning_latest.source_url,
            "source_type": winning_latest.source_type,
            "reason_for_selection": f"Selected value '{current_val}' based on source authority ({winning_latest.authority:.2f}) and temporal currency.",
        }

    # Step 7: Build deterministic history array retaining all variants and full provenance
    final_history = [v.to_dict() for v in sorted_variants]

    # Populate rejected / superseded alternatives
    for v in sorted_variants:
        if v.value != current_val or v.temporal_status in {"superseded", "conflicting_current"}:
            rejected_alternatives.append({
                "alternative_value": v.value,
                "source": v.source_url,
                "source_type": v.source_type,
                "rejection_reason": f"Status '{v.temporal_status}' relative to selected current value.",
                "entity_match": getattr(v, "entity_match_state", "verified_match"),
                "authority": v.authority,
                "temporal_status": v.temporal_status,
            })

    return FactHistory(
        field_name=field_name,
        current_value=current_val,
        current_variant=current_var_dict,
        temporal_status=final_temporal_status,
        has_unresolved_conflict=has_conflict,
        history=final_history,
        detected_changes=[c.to_dict() for c in detected_changes],
        unresolved_conflicts=unresolved_conflicts,
        selected_value_info=selected_value_info,
        rejected_alternatives=rejected_alternatives,
    )


def build_company_temporal_profile(profile: dict[str, Any]) -> TemporalProfile:
    """Synthesize complete multi-year temporal evidence and change events across company facts."""
    org_no = profile.get("organisation_number") or ""
    evidence_dict = profile.get("evidence", {})
    fact_histories: dict[str, FactHistory] = {}
    all_detected_changes: list[dict[str, Any]] = []
    all_unresolved_conflicts: list[dict[str, Any]] = []

    # 1. EMPLOYEES: Aa-registeret bulk vs live vs annual reports vs website
    emp_variants: list[SourceFactVariant] = []
    bulk_emp = profile.get("employees")
    if bulk_emp is not None:
        reg_ev = evidence_dict.get("registry", {})
        emp_variants.append(SourceFactVariant(
            value=bulk_emp,
            source_url=reg_ev.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            source_type="official_registry_bulk",
            retrieved_at=reg_ev.get("retrieved_at") or utc_now(),
            observed_at=reg_ev.get("retrieved_at") or utc_now(),
            authority=1.0,
            evidence_span=f"Aa-registeret registered employee count: {bulk_emp}",
        ))

    # Add any external workforce/website observations
    wf_ev = evidence_dict.get("workforce", {})
    wf_val = wf_ev.get("value") or {}
    for conf_rec in wf_val.get("conflicts", []):
        for v in conf_rec.get("all_variants", []):
            if v.get("source_type") != "official_registry_bulk":
                emp_variants.append(SourceFactVariant(
                    value=v.get("value"),
                    source_url=v.get("source_url", ""),
                    source_type=v.get("source_type", "company_owned"),
                    retrieved_at=v.get("retrieved_at", utc_now()),
                    observed_at=v.get("observed_at") or v.get("retrieved_at", utc_now()),
                    effective_date=v.get("effective_date"),
                    publication_date=v.get("publication_date"),
                    authority=float(v.get("authority", 0.85)),
                    confidence=float(v.get("confidence", 0.9)),
                    evidence_span=v.get("evidence_span", ""),
                    entity_match_state=v.get("entity_match_state", "verified_match"),
                    publishable=v.get("publishable", True),
                ))

    emp_history = resolve_temporal_fact("employees", emp_variants)
    fact_histories["employees"] = emp_history

    # 2. FINANCIALS: Multi-year accounts (revenue, operating_result, annual_result, assets, equity)
    fin_ev = evidence_dict.get("financials", {})
    fin_val = fin_ev.get("value") or {}
    records = fin_val.get("records") or []

    for fin_metric in ("revenue", "operating_result", "annual_result", "assets", "equity", "debt"):
        metric_variants: list[SourceFactVariant] = []
        for r in records:
            val = r.get(fin_metric)
            if val is None:
                continue
            period = r.get("period") or {}
            til_dato = period.get("tilDato")
            year = str(til_dato)[:4] if til_dato else None
            metric_variants.append(SourceFactVariant(
                value=val,
                source_url=fin_ev.get("source_url") or "https://data.brreg.no/regnskapsregisteret/regnskap",
                source_type="official_financials",
                retrieved_at=fin_ev.get("retrieved_at") or utc_now(),
                effective_date=til_dato or year,
                publication_date=r.get("avsluttetDato") or til_dato or year,
                authority=1.0,
                confidence=1.0,
                evidence_span=f"Statutory filing for period ending {til_dato or year}: {fin_metric} = {val}",
            ))
        m_history = resolve_temporal_fact(fin_metric, metric_variants)
        fact_histories[fin_metric] = m_history

    # 3. GOVERNANCE: CEO and Chair roles with registration dates
    roles_ev = evidence_dict.get("roles", {})
    roles_val = roles_ev.get("value") or {}
    roles_list = roles_val.get("roles") or []

    ceo_variants: list[SourceFactVariant] = []
    chair_variants: list[SourceFactVariant] = []

    for r in roles_list:
        name = r.get("name")
        if not name:
            continue
        role_code = r.get("role_code")
        last_changed = r.get("last_changed")
        inactive = r.get("inactive", False)
        authority = 1.0

        variant = SourceFactVariant(
            value=name,
            source_url=roles_ev.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter",
            source_type="official_roles",
            retrieved_at=roles_ev.get("retrieved_at") or utc_now(),
            effective_date=str(last_changed) if last_changed else None,
            authority=authority,
            confidence=1.0,
            evidence_span=f"BRREG role {role_code} registered for {name} on {last_changed or 'N/A'}",
        )
        if role_code == "DAGL" and not inactive:
            ceo_variants.append(variant)
        elif role_code == "LEDE" and not inactive:
            chair_variants.append(variant)

    fact_histories["ceo"] = resolve_temporal_fact("ceo", ceo_variants)
    fact_histories["chair"] = resolve_temporal_fact("chair", chair_variants)

    # 4. IDENTITY & STATUS
    reg_val = (evidence_dict.get("registry", {}).get("value") or {})
    reg_date = reg_val.get("registreringsdatoEnhetsregisteret")
    name_variants = [
        SourceFactVariant(
            value=profile.get("name"),
            source_url=evidence_dict.get("registry", {}).get("source_url") or "",
            source_type="official_registry_bulk",
            retrieved_at=evidence_dict.get("registry", {}).get("retrieved_at") or utc_now(),
            effective_date=str(reg_date) if reg_date else None,
            authority=1.0,
            confidence=1.0,
            evidence_span=f"Registered legal name: {profile.get('name')}",
        )
    ]
    fact_histories["legal_name"] = resolve_temporal_fact("legal_name", name_variants)

    # Collect current facts, detected changes, and unresolved conflicts
    current_facts: dict[str, Any] = {}
    for fname, fhist in fact_histories.items():
        if fhist.current_value is not None:
            current_facts[fname] = {
                "value": fhist.current_value,
                "variant": fhist.current_variant,
                "temporal_status": fhist.temporal_status,
            }
        all_detected_changes.extend(fhist.detected_changes)
        all_unresolved_conflicts.extend(fhist.unresolved_conflicts)

    # Metrics
    total_historical = sum(
        sum(1 for v in fhist.history if v.get("temporal_status") == "historical")
        for fhist in fact_histories.values()
    )
    total_current = len(current_facts)
    total_changes = len(all_detected_changes)
    total_conflicts = len(all_unresolved_conflicts)

    metrics = {
        "current_facts_count": total_current,
        "historical_facts_retained": total_historical,
        "temporal_changes_detected": total_changes,
        "unresolved_conflicts_count": total_conflicts,
    }

    return TemporalProfile(
        organisation_number=org_no,
        current_facts=current_facts,
        fact_histories={k: v.to_dict() for k, v in fact_histories.items()},
        detected_changes=all_detected_changes,
        unresolved_conflicts=all_unresolved_conflicts,
        metrics=metrics,
    )
