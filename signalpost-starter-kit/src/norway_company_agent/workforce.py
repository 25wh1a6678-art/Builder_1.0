from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.parse
import urllib.request
import urllib.error

from .evidence import evidence, utc_now
from .identity import _tokens
from .entity_resolution import resolve_candidate_entity
from .conflict_resolution import SourceFactVariant, resolve_scalar_conflict


CAREER_URL_PATTERNS = re.compile(
    r"(?i)/(?:karriere|jobb|ledige-stillinger|stillinger|stilling|careers|career|work-with-us|jobs)\b"
)
HIRING_TEXT_PATTERNS = re.compile(
    r"(?i)\b(?:vi søker|ledige stillinger|åpne stillinger|bli en del av|jobb hos oss|karriere|we are hiring|open positions|career opportunities)\b"
)


def _digest(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _entity_match(candidate_name: str, target_name: str) -> tuple[bool, float, list[str]]:
    candidate_tokens = set(_tokens(candidate_name))
    target_tokens = set(_tokens(target_name))
    if not candidate_tokens or not target_tokens:
        return False, 0.0, []
    overlap = candidate_tokens & target_tokens
    ratio = len(overlap) / len(target_tokens)
    # If all core target tokens are in candidate or high token overlap
    is_match = target_tokens.issubset(candidate_tokens) or ratio >= 0.8
    return is_match, round(ratio, 3), sorted(overlap)


def extract_website_career_signals(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Inspect verified company website for career links and hiring signals."""
    signals: list[dict[str, Any]] = []
    rejected_entity = 0
    rejected_evidence = 0

    website_ev = profile.get("evidence", {}).get("website", {})
    if website_ev.get("status") != "available":
        return signals, rejected_entity, rejected_evidence

    val = website_ev.get("value") or {}
    assessment = val.get("identity_assessment") or {}
    if not assessment.get("publishable", True):
        # Website itself was rejected by entity resolution
        rejected_entity += 1
        return signals, rejected_entity, rejected_evidence

    org = profile.get("organisation_number")
    pages = val.get("pages") or []
    homepage_url = val.get("final_url") or website_ev.get("source_url") or ""

    for page in pages:
        page_url = page.get("url") or ""
        page_text = page.get("main_text_excerpt") or ""
        page_title = page.get("title") or ""
        combined = f"{page_title} {page_text}"

        is_career_url = bool(CAREER_URL_PATTERNS.search(page_url))
        has_hiring_text = bool(HIRING_TEXT_PATTERNS.search(combined))

        if is_career_url or has_hiring_text:
            if not page_text and not is_career_url:
                rejected_evidence += 1
                continue

            match = HIRING_TEXT_PATTERNS.search(combined)
            span = match.group(0) if match else "Career / recruitment section"
            signals.append({
                "type": "website_hiring_signal",
                "company_identifier": org,
                "title": page_title or "Career & Job Opportunities",
                "source_url": page_url,
                "source_type": "company_owned",
                "source_class": "company_owned",
                "retrieved_at": website_ev.get("retrieved_at") or utc_now(),
                "content_sha256": page.get("content_sha256") or _digest(page_text or page_url),
                "evidence_span": f"Verified website career signal on {page_url}: '{span}'",
                "confidence": 0.95,
            })

    return signals, rejected_entity, rejected_evidence


def extract_official_registry_workforce(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract authoritative workforce counts from Enhetsregisteret and Subunits."""
    signals: list[dict[str, Any]] = []
    org = profile.get("organisation_number")
    reg_ev = profile.get("evidence", {}).get("registry", {})
    raw_reg = reg_ev.get("value") or {}

    employees = profile.get("employees")
    reg_date = raw_reg.get("registreringsdatoAntallAnsatteEnhetsregisteret")
    nav_date = raw_reg.get("registreringsdatoantallansatteNAVAaregisteret")
    has_registered = str(raw_reg.get("harRegistrertAntallAnsatte") or "").lower() == "true"

    if employees is not None:
        span_parts = [f"Antall ansatte: {employees}"]
        if reg_date:
            span_parts.append(f"registrert i Enhetsregisteret: {reg_date}")
        if nav_date:
            span_parts.append(f"registrert i NAV Aa-registeret: {nav_date}")
        signals.append({
            "type": "official_workforce_count",
            "company_identifier": org,
            "measure": "registered_employees",
            "value": employees,
            "source_url": reg_ev.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            "source_type": "official_registry_bulk",
            "source_class": "official_registry_bulk",
            "retrieved_at": reg_ev.get("retrieved_at") or utc_now(),
            "content_sha256": reg_ev.get("content_sha256"),
            "evidence_span": "; ".join(span_parts),
            "confidence": 1.0,
            "effective_at": reg_date or nav_date,
        })
    elif has_registered:
        signals.append({
            "type": "official_workforce_reporting_status",
            "company_identifier": org,
            "measure": "reporting_registered",
            "value": None,
            "source_url": reg_ev.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            "source_type": "official_registry_bulk",
            "source_class": "official_registry_bulk",
            "retrieved_at": reg_ev.get("retrieved_at") or utc_now(),
            "content_sha256": reg_ev.get("content_sha256"),
            "evidence_span": f"harRegistrertAntallAnsatte: true, registrert dato: {reg_date or 'N/A'}",
            "confidence": 1.0,
            "effective_at": reg_date,
        })

    # Subunits (workplaces)
    loc_ev = profile.get("evidence", {}).get("locations", {})
    subunits = (loc_ev.get("value") or {}).get("locations") or []
    if subunits:
        names = [s.get("name") for s in subunits if s.get("name")]
        signals.append({
            "type": "registered_workplaces",
            "company_identifier": org,
            "measure": "workplaces_count",
            "value": len(subunits),
            "workplaces": names,
            "source_url": loc_ev.get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}",
            "source_type": "official_subunits",
            "source_class": "official_subunits",
            "retrieved_at": loc_ev.get("retrieved_at") or utc_now(),
            "content_sha256": loc_ev.get("content_sha256"),
            "evidence_span": f"{len(subunits)} registered workplace(s): {', '.join(names[:3])}",
            "confidence": 1.0,
        })

    return signals


def query_external_job_board(
    company_name: str,
    org_no: str,
    cache_dir: Path | None = None,
    profile: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str, int, int]:
    """Query permitted external job board with caching, multi-signal entity matching, and graceful rate handling."""
    job_openings: list[dict[str, Any]] = []
    rejected_entity = 0
    rejected_evidence = 0
    status = "not_available"

    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"nav-jobs-{org_no}.json"
        if cache_file.exists():
            try:
                cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                return cached_data.get("jobs", []), cached_data.get("status", "available"), cached_data.get("rej_entity", 0), cached_data.get("rej_evidence", 0)
            except Exception:
                pass

    query = urllib.parse.quote(company_name.strip())
    url = f"https://arbeidsplassen.nav.no/stillinger/api/search?q={query}&size=5"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SignalpostResearch/1.0 (Norwegian company research agent)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            hits = data.get("hits", {}).get("hits", [])
            if hits:
                status = "available"
            for hit in hits:
                src = hit.get("_source") or {}
                employer_name = (src.get("employer") or {}).get("name") or src.get("businessName") or ""
                candidate_org = (src.get("employer") or {}).get("orgnr")
                locations = src.get("locationList") or []
                loc_str = locations[0].get("city") or locations[0].get("address") if locations else "Norway"

                # Multi-Signal Entity Resolution Gate
                cand_data = {
                    "id": src.get("uuid"),
                    "name": employer_name,
                    "organisation_number": candidate_org,
                    "address": loc_str,
                }
                if profile:
                    res = resolve_candidate_entity(profile, cand_data)
                    if not res.publishable:
                        rejected_entity += 1
                        continue
                    match_info = res.to_dict()
                    conf = res.confidence
                else:
                    is_match, ratio, _ = _entity_match(employer_name, company_name)
                    if not is_match:
                        rejected_entity += 1
                        continue
                    match_info = {"match_state": "verified_match", "confidence": ratio}
                    conf = ratio

                title = src.get("title")
                uuid = src.get("uuid")
                if not title or not uuid:
                    rejected_evidence += 1
                    continue

                job_url = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
                pub_date = src.get("published")

                job_openings.append({
                    "company_identifier": org_no,
                    "title": title,
                    "employer": employer_name,
                    "location": loc_str,
                    "posted_date": pub_date,
                    "source_url": job_url,
                    "source_type": "official_job_board",
                    "source_class": "job_board",
                    "retrieved_at": utc_now(),
                    "content_sha256": _digest(f"{uuid}|{title}|{employer_name}"),
                    "evidence_span": f"Job posting: '{title}' by '{employer_name}' in {loc_str}. Posted: {pub_date or 'N/A'}",
                    "confidence": min(0.99, max(0.85, conf)),
                    "entity_match_result": match_info,
                })
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            status = "blocked"
        else:
            status = "source_error"
    except Exception:
        status = "source_error"


    if cache_dir:
        try:
            cache_file.write_text(
                json.dumps({
                    "jobs": job_openings,
                    "status": status,
                    "rej_entity": rejected_entity,
                    "rej_evidence": rejected_evidence,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    return job_openings, status, rejected_entity, rejected_evidence


def research_workforce_and_jobs(
    profile: dict[str, Any],
    cache_dir: str | Path | None = None,
    plan: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute complete V2 workforce & jobs research layer."""
    org = profile.get("organisation_number")
    company_name = profile.get("name") or ""
    c_dir = Path(cache_dir) if cache_dir else Path("out/cache/jobs")

    # 1. Official Registry Workforce
    registry_signals = extract_official_registry_workforce(profile)

    # 2. Verified Website Career Signals
    website_signals, web_rej_entity, web_rej_evidence = extract_website_career_signals(profile)

    # 3. External Job Board (Adaptive)
    should_query_jobs = plan.should_execute("workforce_external_jobs") if plan is not None else True
    if should_query_jobs:
        external_jobs, ext_status, ext_rej_entity, ext_rej_evidence = query_external_job_board(
            company_name, org, cache_dir=c_dir, profile=profile
        )
    else:
        external_jobs, ext_status, ext_rej_entity, ext_rej_evidence = [], "skipped_by_planner", 0, 0

    total_rejected_entity = web_rej_entity + ext_rej_entity
    total_rejected_evidence = web_rej_evidence + ext_rej_evidence
    all_job_openings = external_jobs
    all_signals = registry_signals + website_signals

    has_jobs = len(all_job_openings) > 0 or len(website_signals) > 0
    has_workforce = any(s.get("type") == "official_workforce_count" for s in registry_signals)

    if has_jobs:
        hiring_activity = "active_openings_observed"
    elif website_signals:
        hiring_activity = "career_section_observed"
    else:
        hiring_activity = "no_active_openings_observed"

    workforce_status = "available" if (has_workforce or has_jobs) else ext_status if ext_status == "blocked" else "not_available"

    # Conflict resolution for workforce
    conflicts: list[dict[str, Any]] = []
    official_emp = profile.get("employees")
    if official_emp is not None:
        variants = [
            SourceFactVariant(
                value=official_emp,
                source_url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                source_type="official_registry_bulk",
                retrieved_at=utc_now(),
                authority=1.0,
                evidence_span=f"Aa-registeret registered employee count: {official_emp}",
            )
        ]
        conflicts.append(resolve_scalar_conflict("employees", variants).to_dict())

    workforce_payload = {
        "organisation_number": org,
        "company_name": company_name,
        "workforce_signals": all_signals,
        "job_openings": all_job_openings,
        "hiring_activity": hiring_activity,
        "conflicts": conflicts,
        "metrics": {
            "workforce_signals_count": len(all_signals),
            "job_openings_count": len(all_job_openings),
            "rejected_by_entity_resolution": total_rejected_entity,
            "rejected_insufficient_evidence": total_rejected_evidence,
            "conflicts_detected": sum(bool(c.get("has_conflict")) for c in conflicts),
        },
    }


    payload_json = json.dumps(workforce_payload, sort_keys=True, ensure_ascii=False)
    content_sha256 = _digest(payload_json)

    workforce_evidence = evidence(
        "workforce",
        workforce_status,
        "multi_source_workforce_research",
        "internal://engine/workforce/v2",
        value=workforce_payload,
        content_sha256=content_sha256,
        retrieved_at=utc_now(),
        source_row_key=org,
    )

    metrics = {
        "requests": 1 if ext_status not in {"cached"} else 0,
        "workforce_signals_count": len(all_signals),
        "job_openings_count": len(all_job_openings),
        "rejected_by_entity_resolution": total_rejected_entity,
        "rejected_insufficient_evidence": total_rejected_evidence,
    }

    profile["workforce"] = workforce_payload
    profile["evidence"]["workforce"] = workforce_evidence
    return workforce_evidence, metrics
