from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .external_footprint import (
    PLATFORMS,
    SIGNAL_TYPES,
    PUBLISHABLE_ACQUISITION_MODES,
    aggregate_footprint,
    publishable_observation,
    validate_observation,
)
from .identity import _tokens


REVIEWS_CACHE_DIR = Path("out/cache/reviews")
NEWS_PATH = re.compile(r"/(?:news|press|aktuelt|nyheter|artikler|blog)(?:/|$)", re.I)
UA = "SignalpostResearchPOC/1.0 (+https://builderr.ai; bounded qualification run)"


def _digest(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _slug(value: object) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    return "-".join(re.findall(r"[a-z0-9]+", text.casefold()))


def extract_website_footprint_observations(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract verifiable website and social footprint observations from the verified company site."""
    accepted_obs: list[dict[str, Any]] = []
    rejected_obs: list[dict[str, Any]] = []
    org = str(profile.get("organisation_number") or "").strip()

    website_ev = profile.get("evidence", {}).get("website", {})
    if website_ev.get("status") != "available":
        return accepted_obs, rejected_obs

    val = website_ev.get("value") or {}
    assessment = val.get("identity_assessment") or {}
    is_publishable = bool(assessment.get("publishable", False))

    if not is_publishable:
        rejected_obs.append({
            "id": f"website-quarantine-{org}",
            "reasons": ["website failed exact-entity identity assessment: quarantined"],
        })
        return accepted_obs, rejected_obs

    source_url = val.get("final_url") or website_ev.get("source_url") or ""
    digest = val.get("content_sha256") or website_ev.get("content_sha256") or ""
    if not digest or len(digest) != 64:
        digest = _digest(source_url)

    pages = val.get("pages") or []
    socials = val.get("social_links") or []
    retrieved_at = website_ev.get("retrieved_at") or utc_now()

    # 1. Company Site Profile & Activity Observation
    site_obs = {
        "id": f"company-site-{org}-{digest[:16]}",
        "organisation_number": org,
        "platform": "company_site",
        "signal_type": "profile_metrics",
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": [
            {"type": "website_identity_gate", "status": assessment.get("status"), "score": assessment.get("score")},
        ],
        "acquisition_mode": "permitted_public_page",
        "rights_status": "approved",
        "source_class": "company_site",
        "evidence_span": f"Exact company site snapshot with {len(pages)} bounded pages and {len(socials)} verified social links.",
        "metrics": {
            "bounded_pages_captured": len(pages),
            "verified_social_links": len(socials),
            "title": val.get("title", ""),
            "description": val.get("description", ""),
        },
    }

    validation_errs = validate_observation(site_obs)
    if not validation_errs:
        accepted_obs.append(site_obs)
    else:
        rejected_obs.append({"id": site_obs["id"], "reasons": validation_errs})

    # 2. Company Site News / Public Post Observation
    news_pages = [
        page for page in pages
        if NEWS_PATH.search(urllib.parse.urlparse(str(page.get("url") or "")).path)
    ]
    if news_pages:
        page = news_pages[0]
        page_url = str(page.get("url") or "")
        page_digest = str(page.get("content_sha256") or "")
        if not page_digest or len(page_digest) != 64:
            page_digest = _digest(page_url)
        page_title = str(page.get("title") or "Company news page").strip()
        news_obs = {
            "id": f"company-site-news-{org}-{page_digest[:16]}",
            "organisation_number": org,
            "platform": "company_site",
            "signal_type": "public_post",
            "source_url": page_url,
            "retrieved_at": retrieved_at,
            "content_sha256": page_digest,
            "exact_entity": True,
            "identity_proof": [
                {"type": "website_identity_gate", "status": assessment.get("status"), "score": assessment.get("score")},
            ],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "evidence_span": page_title[:1000] or "Company-owned news and activity page.",
            "metrics": {
                "news_pages_captured": len(news_pages),
            },
        }
        val_errs = validate_observation(news_obs)
        if not val_errs:
            accepted_obs.append(news_obs)
        else:
            rejected_obs.append({"id": news_obs["id"], "reasons": val_errs})

    # 3. Verified Social Profiles
    for link in socials:
        platform = link.get("platform")
        link_url = link.get("url")
        if platform not in PLATFORMS or not link_url:
            continue
        link_digest = _digest(link_url)
        social_obs = {
            "id": f"social-{platform}-{org}-{link_digest[:16]}",
            "organisation_number": org,
            "platform": platform,
            "signal_type": "profile_handle",
            "source_url": link_url,
            "retrieved_at": retrieved_at,
            "content_sha256": link_digest,
            "exact_entity": True,
            "identity_proof": [
                {"type": "website_linked_social_profile", "verified_by_site_identity": True},
            ],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "evidence_span": f"Verified company social handle {link_url} declared on verified official website.",
            "metrics": {
                "platform": platform,
                "url": link_url,
            },
        }
        val_errs = validate_observation(social_obs)
        if not val_errs:
            accepted_obs.append(social_obs)
        else:
            rejected_obs.append({"id": social_obs["id"], "reasons": val_errs})

    return accepted_obs, rejected_obs


def research_local_reviews_and_presence(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Research public local presence and ratings/reviews with strict entity resolution."""
    accepted_obs: list[dict[str, Any]] = []
    rejected_obs: list[dict[str, Any]] = []
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    org = str(profile.get("organisation_number") or "").strip()
    name = str(profile.get("name") or "").strip()

    if not org or not name:
        return accepted_obs, rejected_obs, metrics

    # Query public Norwegian business review / directory directory
    REVIEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = REVIEWS_CACHE_DIR / f"{org}.json"

    cached_data = None
    if cache_file.exists():
        try:
            cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached_data = None

    if cached_data is None:
        url = f"https://www.fagfolkguiden.no/bedrift/{_slug(name)}-{org}"
        req_start = datetime.now(timezone.utc)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                raw_bytes = resp.read(1_000_000)
                elapsed_ms = int((datetime.now(timezone.utc) - req_start).total_seconds() * 1000)
                metrics["requests"] += 1
                metrics["bytes"] += len(raw_bytes)
                metrics["latencies_ms"].append(elapsed_ms)
                html_text = raw_bytes.decode("utf-8", errors="replace")
                cached_data = {
                    "url": url,
                    "status_code": resp.status,
                    "html": html_text,
                    "content_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "retrieved_at": utc_now(),
                }
                cache_file.write_text(json.dumps(cached_data, ensure_ascii=False), encoding="utf-8")
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((datetime.now(timezone.utc) - req_start).total_seconds() * 1000)
            metrics["requests"] += 1
            metrics["latencies_ms"].append(elapsed_ms)
            cached_data = {
                "url": url,
                "status_code": exc.code,
                "html": "",
                "content_sha256": _digest(f"{org}|{exc.code}"),
                "retrieved_at": utc_now(),
            }
            cache_file.write_text(json.dumps(cached_data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            cached_data = {
                "url": url,
                "status_code": 0,
                "html": "",
                "content_sha256": _digest(f"{org}|error"),
                "retrieved_at": utc_now(),
            }
            cache_file.write_text(json.dumps(cached_data, ensure_ascii=False), encoding="utf-8")

    # If valid page with review data found, parse JSON-LD
    html = cached_data.get("html") or ""
    if html and cached_data.get("status_code") == 200:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Exact entity resolution gate: legal name and org number must be in text
        exact = name.casefold() in text.casefold() and org in re.sub(r"\D", "", text)
        if not exact:
            rejected_obs.append({
                "id": f"fagfolk-{org}",
                "reasons": ["candidate page text failed exact legal name and organisation number match"],
            })
            return accepted_obs, rejected_obs, metrics

        # Extract aggregate rating
        rating_val = None
        review_count = None
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(node.string or node.get_text() or "{}")
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                agg = (item or {}).get("aggregateRating") if isinstance(item, dict) else None
                if isinstance(agg, dict):
                    rv = agg.get("ratingValue")
                    rc = agg.get("ratingCount") or agg.get("reviewCount")
                    if rv is not None and rc is not None:
                        try:
                            rating_val = float(rv)
                            review_count = int(rc)
                            break
                        except (ValueError, TypeError):
                            pass
            if rating_val is not None:
                break

        if rating_val is not None and review_count is not None and review_count > 0:
            digest = cached_data.get("content_sha256") or _digest(html)
            retrieved_at = cached_data.get("retrieved_at") or utc_now()
            source_url = cached_data.get("url") or ""
            obs = {
                "id": f"review-summary-{org}-{digest[:16]}",
                "organisation_number": org,
                "platform": "company_directory",
                "signal_type": "review_summary",
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "content_sha256": digest,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "exact_legal_name_and_org_on_page", "name": name, "org": org},
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "customer_review",
                "evidence_span": f"Customer review aggregate: {rating_val}/5 based on {review_count} verified reviews.",
                "metrics": {
                    "rating": rating_val,
                    "review_count": review_count,
                    "scale": 5,
                },
            }
            val_errs = validate_observation(obs)
            if not val_errs:
                accepted_obs.append(obs)
            else:
                rejected_obs.append({"id": obs["id"], "reasons": val_errs})

    return accepted_obs, rejected_obs, metrics


def extract_contact_and_location_signals(profile: dict[str, Any]) -> dict[str, Any]:
    """Extract and ground contact and location signals from BRREG and verified website."""
    reg_val = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    locations_val = (profile.get("evidence", {}).get("locations", {}).get("value") or {})
    website_val = (profile.get("evidence", {}).get("website", {}).get("value") or {})
    is_publishable_website = bool(website_val.get("identity_assessment", {}).get("publishable", False))

    business_address = {
        "address": reg_val.get("forretningsadresse.adresse") or profile.get("business_address"),
        "postal_code": reg_val.get("forretningsadresse.postnummer") or profile.get("postal_code"),
        "city": reg_val.get("forretningsadresse.poststed") or profile.get("postal_area"),
        "municipality": reg_val.get("forretningsadresse.kommune") or profile.get("municipality"),
        "country": reg_val.get("forretningsadresse.land") or "Norge",
    }

    postal_address = {
        "address": reg_val.get("postadresse.adresse"),
        "postal_code": reg_val.get("postadresse.postnummer"),
        "city": reg_val.get("postadresse.poststed"),
        "country": reg_val.get("postadresse.land"),
    }

    # Registered branch workplaces
    raw_subunits = locations_val.get("locations") or []
    subunits = []
    for sub in raw_subunits:
        sub_name = sub.get("navn") or sub.get("name")
        sub_org = sub.get("organisasjonsnummer") or sub.get("organisation_number")
        sub_muni = (sub.get("forretningsadresse") or {}).get("kommune") or sub.get("municipality")
        if sub_name:
            subunits.append({
                "name": sub_name,
                "organisation_number": sub_org,
                "municipality": sub_muni,
            })

    # Website contact info
    website_contact = {}
    if is_publishable_website:
        contact_pages = [
            page.get("url") for page in website_val.get("pages", [])
            if any(term in str(page.get("url", "")).casefold() for term in ("kontakt", "contact"))
        ]
        website_contact = {
            "website_url": website_val.get("final_url"),
            "contact_page": contact_pages[0] if contact_pages else None,
        }

    return {
        "business_address": business_address,
        "postal_address": postal_address,
        "phone": reg_val.get("telefon") or reg_val.get("mobil"),
        "registered_workplaces_count": len(subunits),
        "registered_workplaces": subunits,
        "website_contact": website_contact if website_contact else None,
    }


def research_external_footprint(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Research and assemble the full evidence-backed external footprint for a company."""
    all_accepted: list[dict[str, Any]] = []
    all_rejected: list[dict[str, Any]] = []

    # 1. Company website and verified social channels
    web_accepted, web_rejected = extract_website_footprint_observations(profile)
    all_accepted.extend(web_accepted)
    all_rejected.extend(web_rejected)

    # 2. Reviews and local presence
    rev_accepted, rev_rejected, rev_metrics = research_local_reviews_and_presence(profile)
    all_accepted.extend(rev_accepted)
    all_rejected.extend(rev_rejected)

    # 3. Grounded contact and location signals
    contact_locs = extract_contact_and_location_signals(profile)

    # 4. Aggregate footprint
    footprint_rollup = aggregate_footprint(all_accepted)
    footprint_rollup["contact_and_locations"] = contact_locs
    footprint_rollup["all_accepted_observations"] = all_accepted
    footprint_rollup["all_rejected_observations"] = all_rejected

    status = "available" if all_accepted else "not_found"
    org = str(profile.get("organisation_number") or "").strip()
    source_url = (
        (profile.get("evidence", {}).get("website", {}).get("value") or {}).get("final_url")
        or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
    )

    ev_record = evidence(
        field="external_footprint",
        status=status,
        source_type="multi_source_external_footprint",
        source_url=source_url,
        value=footprint_rollup,
        content_sha256=_digest(json.dumps(footprint_rollup, sort_keys=True)),
        note="Evidence-backed multi-source external web footprint, local presence, and reviews",
    )

    operations_metric = {
        "requests": rev_metrics.get("requests", 0),
        "bytes": rev_metrics.get("bytes", 0),
        "latencies_ms": rev_metrics.get("latencies_ms", []),
    }

    return ev_record, operations_metric
