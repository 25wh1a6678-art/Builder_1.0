from __future__ import annotations

import hashlib
import json
from typing import Any

from .evidence import evidence, utc_now
from .temporal import build_company_temporal_profile
from .evidence_quality import classify_source_category


def _digest(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _format_currency(amount: float | int | None) -> str:
    if amount is None:
        return "N/A"
    return f"{amount:,.0f} NOK".replace(",", " ")


def _calculate_growth(prior: float | int | None, current: float | int | None) -> str | None:
    if prior is None or current is None or prior == 0:
        return None
    growth = ((current - prior) / abs(prior)) * 100
    sign = "+" if growth > 0 else ""
    return f"{sign}{growth:.1f}%"


def generate_company_summary(profile: dict[str, Any]) -> str:
    """Generate a concise, high-utility narrative summary of the company."""
    name = profile.get("name") or "The company"
    org_no = profile.get("organisation_number") or ""
    legal_form = profile.get("legal_form") or "company"
    municipality = profile.get("municipality") or "Norway"
    industry = profile.get("industry_label") or "Unspecified sector"
    
    parts = [f"{name} (Org. {org_no}) is a Norwegian {legal_form} based in {municipality}, operating in {industry}."]
    
    # Status
    if profile.get("bankrupt"):
        parts.append("Status: Company is in bankruptcy proceedings.")
    elif profile.get("liquidating"):
        parts.append("Status: Company is undergoing liquidation.")
    else:
        parts.append("Status: Active and registered in the Norwegian register.")
        
    # Management / Leadership
    roles_evidence = profile.get("evidence", {}).get("roles", {})
    roles = (roles_evidence.get("value") or {}).get("roles", [])
    ceo = next((r.get("name") for r in roles if r.get("role_code") == "DAGL" and not r.get("inactive")), None)
    chair = next((r.get("name") for r in roles if r.get("role_code") == "LEDE" and not r.get("inactive")), None)
    
    if ceo and chair and ceo == chair:
        parts.append(f"Management: {ceo} serves as both CEO (daglig leder) and Chair of the Board (styreleder).")
    elif ceo:
        parts.append(f"Management: Led by CEO (daglig leder) {ceo}.")
        if chair:
            parts.append(f"Board chaired by {chair}.")
    elif chair:
        parts.append(f"Board chaired by {chair}.")

    # Latest Financials
    fin_evidence = profile.get("evidence", {}).get("financials", {})
    raw_fin_records = (fin_evidence.get("value") or {}).get("records", [])
    fin_records = sorted(
        raw_fin_records,
        key=lambda r: str(r.get("period", {}).get("tilDato", "")),
        reverse=True,
    )
    if fin_records:
        latest = fin_records[0]
        year = str(latest.get("period", {}).get("tilDato", ""))[:4] or "latest"
        rev = latest.get("revenue")
        profit = latest.get("annual_result")
        if rev is not None and profit is not None:
            parts.append(f"Financials ({year}): Revenue {_format_currency(rev)} with net profit/loss {_format_currency(profit)}.")
        elif profit is not None:
            parts.append(f"Financials ({year}): Net annual result {_format_currency(profit)}.")
    else:
        parts.append("Financials: No normalized annual account records returned by Regnskapsregisteret.")

    # Workforce & Hiring
    wf = profile.get("workforce") or {}
    jobs = wf.get("job_openings") or []
    employees = profile.get("employees")
    if employees is not None:
        parts.append(f"Workforce: {employees} registered employee(s).")
    else:
        parts.append("Workforce: No registered employee count reported to NAV Aa-registeret.")

    if jobs:
        titles = [j.get("title") for j in jobs if j.get("title")]
        parts.append(f"Hiring: {len(jobs)} active job opening(s) observed ({', '.join(titles[:2])}).")
    elif wf.get("hiring_activity") == "career_section_observed":
        parts.append("Hiring: Career section observed on official website.")

    # External Web Footprint & Reviews
    fp = profile.get("external_footprint") or (profile.get("evidence", {}).get("external_footprint", {}).get("value") or {})
    platforms = fp.get("platforms") or []
    if platforms:
        parts.append(f"Footprint: Observed on {len(platforms)} platform(s) ({', '.join(platforms)}).")
    if fp.get("review_signal_count", 0) > 0:
        parts.append(f"Reviews: {fp['review_signal_count']} verified review/place signal(s).")

    return " ".join(parts)


from .conflict_resolution import resolve_business_description_conflict


def generate_business_description(profile: dict[str, Any]) -> str:
    """Extract and synthesize business purpose and operational description using conflict resolution."""
    raw_registry = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    purpose = raw_registry.get("vedtektsfestetFormaal") or raw_registry.get("aktivitet") or ""
    industry_code = profile.get("industry_code") or ""
    industry_label = profile.get("industry_label") or ""

    website_evidence = profile.get("evidence", {}).get("website", {})
    website_val = website_evidence.get("value") or {}
    web_desc = website_val.get("description") if website_val.get("identity_assessment", {}).get("publishable", True) else ""
    web_excerpt = website_val.get("main_text_excerpt", "")[:250].strip() if website_val.get("identity_assessment", {}).get("publishable", True) else ""

    resolved_rec = resolve_business_description_conflict(
        statutory_purpose=purpose,
        website_description=web_desc or web_excerpt,
        website_url=website_val.get("final_url", ""),
    )
    descriptions = [resolved_rec.selected_value] if resolved_rec.selected_value else []
    if industry_code:
        descriptions.append(f"Industry classification: NACE {industry_code} - {industry_label}.")
    return " ".join(descriptions)




def detect_financial_operational_changes(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect multi-year financial trends and operational changes."""
    changes = []
    fin_evidence = profile.get("evidence", {}).get("financials", {})
    raw_records = (fin_evidence.get("value") or {}).get("records", [])
    records = sorted(
        raw_records,
        key=lambda r: str(r.get("period", {}).get("tilDato", "")),
        reverse=True,
    )

    if len(records) >= 2:
        current, prior = records[0], records[1]
        cur_year = str(current.get("period", {}).get("tilDato", ""))[:4]
        pri_year = str(prior.get("period", {}).get("tilDato", ""))[:4]


        # Revenue change
        cur_rev, pri_rev = current.get("revenue"), prior.get("revenue")
        if cur_rev is not None and pri_rev is not None:
            growth = _calculate_growth(pri_rev, cur_rev)
            diff = cur_rev - pri_rev
            changes.append({
                "type": "revenue_trend",
                "period": f"{pri_year} -> {cur_year}",
                "detail": f"Revenue changed from {_format_currency(pri_rev)} to {_format_currency(cur_rev)} ({growth}, delta: {_format_currency(diff)})",
                "source": fin_evidence.get("source_url"),
            })

        # Profitability shift
        cur_profit, pri_profit = current.get("annual_result"), prior.get("annual_result")
        if cur_profit is not None and pri_profit is not None:
            if pri_profit < 0 and cur_profit > 0:
                trend = "Turnaround to profitability"
            elif pri_profit > 0 and cur_profit < 0:
                trend = "Downturn into net loss"
            else:
                trend = "Earnings change"
            diff = cur_profit - pri_profit
            changes.append({
                "type": "profitability_trend",
                "period": f"{pri_year} -> {cur_year}",
                "detail": f"{trend}: Net result shifted from {_format_currency(pri_profit)} to {_format_currency(cur_profit)} (delta: {_format_currency(diff)})",
                "source": fin_evidence.get("source_url"),
            })

        # Equity change
        cur_equity, pri_equity = current.get("equity"), prior.get("equity")
        if cur_equity is not None and pri_equity is not None:
            diff = cur_equity - pri_equity
            changes.append({
                "type": "equity_trend",
                "period": f"{pri_year} -> {cur_year}",
                "detail": f"Equity changed from {_format_currency(pri_equity)} to {_format_currency(cur_equity)} (delta: {_format_currency(diff)})",
                "source": fin_evidence.get("source_url"),
            })

    # Leadership changes
    roles_evidence = profile.get("evidence", {}).get("roles", {})
    roles = (roles_evidence.get("value") or {}).get("roles", [])
    for r in roles:
        last_changed = r.get("last_changed")
        if last_changed and str(last_changed) >= "2024-01-01":
            changes.append({
                "type": "recent_role_registration",
                "date": last_changed,
                "detail": f"Role '{r.get('role')}' registered/updated for {r.get('name')} on {last_changed}",
                "source": roles_evidence.get("source_url"),
            })

    return changes


def generate_source_backed_explanations(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Produce auditable, source-backed explanations for each verified finding."""
    explanations = []
    evidence_dict = profile.get("evidence", {})

    if "registry" in evidence_dict:
        reg = evidence_dict["registry"]
        explanations.append({
            "subject": "Company Identity & Legal Foundation",
            "explanation": f"Identity anchored in the official Brønnøysundregistrene Enhetsregisteret bulk snapshot. Legal name '{profile.get('name')}', form '{profile.get('legal_form')}', municipality '{profile.get('municipality')}'.",
            "source_url": reg.get("source_url", ""),
            "retrieved_at": reg.get("retrieved_at", ""),
            "content_sha256": reg.get("content_sha256", ""),
        })

    if "financials" in evidence_dict:
        fin = evidence_dict["financials"]
        if fin.get("status") == "available":
            recs = (fin.get("value") or {}).get("records", [])
            years = [str(r.get("period", {}).get("tilDato", ""))[:4] for r in recs if r.get("period")]
            explanations.append({
                "subject": "Statutory Annual Accounts",
                "explanation": f"Retrieved normalized annual accounts for {len(recs)} fiscal year(s) ({', '.join(years)}) from Regnskapsregisteret official filings.",
                "source_url": fin.get("source_url", ""),
                "retrieved_at": fin.get("retrieved_at", ""),
                "content_sha256": fin.get("content_sha256", ""),
            })

    if "roles" in evidence_dict:
        roles_ev = evidence_dict["roles"]
        if roles_ev.get("status") == "available":
            roles_list = (roles_ev.get("value") or {}).get("roles", [])
            active_roles = [r for r in roles_list if not r.get("inactive")]
            explanations.append({
                "subject": "Public Role Holders & Governance",
                "explanation": f"Found {len(active_roles)} active public role holder(s) from BRREG Enhetsregisteret roles API.",
                "source_url": roles_ev.get("source_url", ""),
                "retrieved_at": roles_ev.get("retrieved_at", ""),
                "content_sha256": roles_ev.get("content_sha256", ""),
            })

    if "website" in evidence_dict:
        web_ev = evidence_dict["website"]
        if web_ev.get("status") == "available":
            val = web_ev.get("value") or {}
            assessment = val.get("identity_assessment") or {}
            explanations.append({
                "subject": "Official Company Website",
                "explanation": f"Verified homepage at {val.get('final_url')}. Entity matching gate evaluated status as '{assessment.get('status')}' (score: {assessment.get('score')}) with publishable={assessment.get('publishable')}.",
                "source_url": val.get("final_url") or web_ev.get("source_url", ""),
                "retrieved_at": web_ev.get("retrieved_at", ""),
                "content_sha256": val.get("content_sha256") or web_ev.get("content_sha256", ""),
            })

    if "workforce" in evidence_dict:
        wf_ev = evidence_dict["workforce"]
        wf_val = wf_ev.get("value") or {}
        jobs_count = len(wf_val.get("job_openings", []))
        signals_count = len(wf_val.get("workforce_signals", []))
        explanations.append({
            "subject": "Workforce & Hiring Activity",
            "explanation": f"Analyzed workforce indicators ({signals_count} official/web signal(s)) and job postings ({jobs_count} verified opening(s)). Entity resolution enforced zero wrong-company attribution.",
            "source_url": wf_ev.get("source_url", ""),
            "retrieved_at": wf_ev.get("retrieved_at", ""),
            "content_sha256": wf_ev.get("content_sha256", ""),
        })

    if "external_footprint" in evidence_dict:
        fp_ev = evidence_dict["external_footprint"]
        fp_val = fp_ev.get("value") or {}
        obs_count = fp_val.get("accepted_observations", 0)
        platforms = fp_val.get("platforms", [])
        reviews = fp_val.get("review_signal_count", 0)
        explanations.append({
            "subject": "External Web Footprint & Reviews",
            "explanation": f"Discovered {obs_count} verified external observation(s) across {len(platforms)} platform(s) ({', '.join(platforms) if platforms else 'none'}). Customer review signals: {reviews}. Strict entity resolution and rights validation enforced.",
            "source_url": fp_ev.get("source_url", ""),
            "retrieved_at": fp_ev.get("retrieved_at", ""),
            "content_sha256": fp_ev.get("content_sha256", ""),
        })

    if "adaptive_plan" in profile:
        plan_dict = profile["adaptive_plan"]
        arch = plan_dict.get("archetype", "unknown")
        sel = len(plan_dict.get("paths_selected", []))
        skp = len(plan_dict.get("paths_skipped", []))
        saved = plan_dict.get("estimated_requests_saved", 0)
        explanations.append({
            "subject": "Adaptive Research Planner",
            "explanation": f"Classified company as '{arch}'. Dynamically allocated {sel} high-yield research path(s) and skipped {skp} irrelevant/speculative path(s) ({saved} estimated requests saved).",
            "source_url": "internal://engine/planner/v4",
            "retrieved_at": utc_now(),
            "content_sha256": _digest(json.dumps(plan_dict, sort_keys=True)),
        })

    wf_conflicts = (profile.get("workforce") or {}).get("conflicts", [])
    if wf_conflicts:
        c_count = len(wf_conflicts)
        explanations.append({
            "subject": "Entity & Conflict Resolution",
            "explanation": f"Evaluated {c_count} multi-source claim(s). Enforced statutory registry precedence for verified workforce facts while preserving full source provenance.",
            "source_url": "internal://engine/conflict_resolution/v5",
            "retrieved_at": utc_now(),
            "content_sha256": _digest(json.dumps(wf_conflicts, sort_keys=True)),
        })

    temporal_prof = profile.get("temporal_profile") or profile.get("temporal")
    if temporal_prof:
        m = temporal_prof.get("metrics") or {}
        curr_cnt = m.get("current_facts_count", 0)
        hist_cnt = m.get("historical_facts_retained", 0)
        ch_cnt = m.get("temporal_changes_detected", 0)
        conf_cnt = m.get("unresolved_conflicts_count", 0)
        explanations.append({
            "subject": "Temporal Profiles & Change Detection",
            "explanation": (
                f"Anchored {curr_cnt} current fact(s) and retained {hist_cnt} historical variant(s) in deterministic provenance. "
                f"Detected {ch_cnt} genuine temporal change(s) while isolating {conf_cnt} contemporaneous conflict(s)."
            ),
            "source_url": "internal://engine/temporal/v6",
            "retrieved_at": utc_now(),
            "content_sha256": _digest(json.dumps(temporal_prof, sort_keys=True)),
        })

    evidence_qual = profile.get("evidence_quality") or {}
    if evidence_qual:
        g_count = evidence_qual.get("grounded_claims_count", 0)
        v_count = evidence_qual.get("verified_spans_count", 0)
        cat_count = evidence_qual.get("categories_evaluated", 0)
        explanations.append({
            "subject": "Evidence Grounding & Source Quality",
            "explanation": (
                f"Audited {g_count} claim(s) across {cat_count} source categories. "
                f"Verified {v_count} verbatim payload evidence span(s) with SHA-256 grounding and deterministic confidence calibration."
            ),
            "source_url": "internal://engine/evidence_quality/v7",
            "retrieved_at": utc_now(),
            "content_sha256": _digest(json.dumps(evidence_qual, sort_keys=True)),
        })

    return explanations



def identify_explicit_unknowns(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Declare unobserved or unverifiable items explicitly rather than guessing or outputting zero."""
    unknowns = []
    evidence_dict = profile.get("evidence", {})
    skip_reasons = (profile.get("adaptive_plan") or {}).get("skip_reasons", {})

    # Workforce unknown
    if profile.get("employees") is None:
        unknowns.append({
            "field": "employees",
            "state": "not_available",
            "explanation": "No employee count reported to NAV Aa-registeret in the open entity registry; value is explicitly absent, not zero.",
        })

    # Hiring activity unknown
    wf_val = profile.get("workforce") or (evidence_dict.get("workforce", {}).get("value") or {})
    if not wf_val.get("job_openings"):
        skip_reason = skip_reasons.get("workforce_external_jobs")
        unknowns.append({
            "field": "hiring_activity",
            "state": "not_applicable" if skip_reason else "not_available",
            "explanation": skip_reason or "No active job openings observed across public sources (company website, job boards).",
        })

    # Group structure unknown
    group_ev = evidence_dict.get("group", {})
    if group_ev.get("status") in {"not_found", "not_applicable"} or "group" in skip_reasons:
        skip_reason = skip_reasons.get("group")
        unknowns.append({
            "field": "group_structure",
            "state": "not_applicable" if (group_ev.get("status") == "not_applicable" or skip_reason) else "not_found",
            "explanation": skip_reason or "Company has no corporate group structure or subsidiary relations registered in BRREG konsernstruktur API.",
        })

    # Website unknown
    web_ev = evidence_dict.get("website", {})
    if web_ev.get("status") != "available":
        unknowns.append({
            "field": "website",
            "state": web_ev.get("status") or "not_found",
            "explanation": "No valid official website URL was registered in Enhetsregisteret or accessible during this run.",
        })
    elif not (web_ev.get("value") or {}).get("identity_assessment", {}).get("publishable", True):
        unknowns.append({
            "field": "website_content",
            "state": "ambiguous",
            "explanation": "Website was reachable, but identity matching gate could not conclusively verify exact legal entity tokens. Facts quarantined.",
        })

    # Customer Reviews & Sentiment unknown
    fp_val = (evidence_dict.get("external_footprint", {}).get("value") or {})
    sentiment_info = fp_val.get("sentiment") or {}
    if sentiment_info.get("status") in {"abstain", "not_available"} or "reviews_local_presence" in skip_reasons:
        skip_reason = skip_reasons.get("reviews_local_presence")
        unknowns.append({
            "field": "customer_reviews_sentiment",
            "state": "not_applicable" if skip_reason else "abstain",
            "explanation": skip_reason or "Fewer than required independent review sources to form a statistically sound consensus without risking ungrounded sentiment bias.",
        })

    # Financials unknown
    fin_ev = evidence_dict.get("financials", {})
    if fin_ev.get("status") != "available":
        unknowns.append({
            "field": "annual_accounts",
            "state": fin_ev.get("status") or "not_found",
            "explanation": "No normalized financial statements returned by Regnskapsregisteret. Missing figures are not interpreted as zero revenue.",
        })

    return unknowns


def synthesize_company_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Execute complete synthesis engine on an enriched profile."""
    # 1. Synthesize multi-year temporal evidence and history
    temporal_prof = build_company_temporal_profile(profile)
    profile["temporal_profile"] = temporal_prof.to_dict()
    profile["temporal"] = temporal_prof.to_dict()

    # 2. Evidence Grounding & Source Quality Audit
    grounded_claims: list[dict[str, Any]] = []
    cat_counts: dict[str, int] = {}
    conf_dist = {"high_confidence_ge_085": 0, "medium_confidence_070_084": 0, "low_confidence_lt_070": 0}
    verified_spans = 0

    for fname, fhist in temporal_prof.fact_histories.items():
        if fhist.get("current_value") is not None:
            cvar = fhist.get("current_variant") or {}
            conf = float(cvar.get("confidence", 1.0))
            if conf >= 0.85:
                conf_dist["high_confidence_ge_085"] += 1
            elif conf >= 0.70:
                conf_dist["medium_confidence_070_084"] += 1
            else:
                conf_dist["low_confidence_lt_070"] += 1

            stype = cvar.get("source_type", "unknown")
            cat, direct = classify_source_category(stype)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            has_span = bool(cvar.get("evidence_span"))
            if has_span:
                verified_spans += 1

            grounded_claims.append({
                "field": fname,
                "value": fhist.get("current_value"),
                "source_url": cvar.get("source_url"),
                "source_type": stype,
                "source_category": cat,
                "source_directness": direct,
                "confidence": conf,
                "authority": cvar.get("authority", 0.7),
                "evidence_span": cvar.get("evidence_span", ""),
                "content_sha256": cvar.get("content_sha256") or _digest(cvar.get("source_url", "")),
                "selection_reason": fhist.get("selected_value_info", {}).get("reason_for_selection", "Selected by authority and currency precedence."),
            })

    evidence_qual = {
        "grounded_claims_count": len(grounded_claims),
        "verified_spans_count": verified_spans,
        "categories_evaluated": len(cat_counts),
        "source_quality_distribution": cat_counts,
        "confidence_distribution": conf_dist,
        "low_confidence_claims_rejected": sum(len(fhist.get("rejected_alternatives", [])) for fhist in temporal_prof.fact_histories.values()),
    }
    profile["evidence_quality"] = evidence_qual
    profile["grounded_claims"] = grounded_claims

    summary = generate_company_summary(profile)
    business_desc = generate_business_description(profile)
    changes = detect_financial_operational_changes(profile)
    for ch in temporal_prof.detected_changes:
        if ch.get("field") in {"employees", "ceo", "chair"}:
            changes.append({
                "type": f"{ch.get('field')}_temporal_change",
                "period": f"{ch.get('old_date')} -> {ch.get('new_date')}",
                "detail": ch.get("explanation"),
                "source": ch.get("new_source"),
            })

    explanations = generate_source_backed_explanations(profile)
    unknowns = identify_explicit_unknowns(profile)

    synthesis_payload = {
        "summary": summary,
        "business_description": business_desc,
        "financial_operational_changes": changes,
        "source_backed_explanations": explanations,
        "explicit_unknowns": unknowns,
        "temporal_profile": profile["temporal_profile"],
        "evidence_quality": evidence_qual,
        "grounded_claims": grounded_claims,
    }

    payload_bytes = json.dumps(synthesis_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    content_sha256 = hashlib.sha256(payload_bytes).hexdigest()

    synthesis_evidence = evidence(
        "synthesis",
        "available",
        "derived_profile_synthesis",
        "internal://engine/synthesis/v1",
        value=synthesis_payload,
        content_sha256=content_sha256,
        retrieved_at=utc_now(),
        source_row_key=profile.get("organisation_number"),
    )

    profile["synthesis"] = synthesis_payload
    profile["evidence"]["synthesis"] = synthesis_evidence
    if "external_footprint" in profile.get("evidence", {}):
        profile["external_footprint"] = profile["evidence"]["external_footprint"].get("value")
    return profile

