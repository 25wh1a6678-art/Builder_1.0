from __future__ import annotations

import difflib
import re
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .identity import _tokens


MatchState = Literal["verified_match", "probable_match", "ambiguous", "rejected"]

GENERIC_NAME_TOKENS = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "ltd", "limited", "inc", "plc", "norge", "norway", "gruppen",
    "group", "holding", "eiendom", "invest", "service", "drift", "avd", "avdeling",
}


def _clean_digits(val: Any) -> str:
    return re.sub(r"\D", "", str(val or ""))


def _clean_phone(val: Any) -> str:
    digits = _clean_digits(val)
    if digits.startswith("47") and len(digits) > 8:
        digits = digits[2:]
    return digits[-8:] if len(digits) >= 8 else digits


def _registered_domain(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urllib.parse.urlparse(raw).hostname or "").casefold().removeprefix("www.")
        return host
    except Exception:
        return ""


@dataclass
class EntityResolutionResult:
    candidate_id: str
    match_state: MatchState
    confidence: float
    publishable: bool
    matched_signals: list[str] = field(default_factory=list)
    rejected_signals: list[str] = field(default_factory=list)
    explanation: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_candidate_entity(
    profile: dict[str, Any],
    candidate: dict[str, Any],
) -> EntityResolutionResult:
    """Multi-signal deterministic entity resolution between a company profile and an external candidate.

    Evaluates:
    - organisation number (exact 9-digit anchor)
    - distinctive company legal name tokens
    - registered domain / website URL
    - street address & postal code / city
    - municipality
    - telephone / mobile number
    - legal form
    - registered subunits (underenheter)
    - parent / corporate group relationships
    """
    candidate_id = str(candidate.get("id") or candidate.get("url") or candidate.get("title") or "candidate")
    target_org = _clean_digits(profile.get("organisation_number"))
    candidate_org = _clean_digits(candidate.get("organisation_number"))

    # Extract target signals
    target_name = str(profile.get("name") or "").strip()
    target_tokens = [t for t in _tokens(target_name) if t not in GENERIC_NAME_TOKENS]
    target_form = str(profile.get("legal_form") or "").upper()
    target_municipality = str(profile.get("municipality") or "").strip().casefold()

    reg_val = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    target_street = str(reg_val.get("forretningsadresse.adresse") or profile.get("business_address") or "").casefold()
    target_postcode = str(reg_val.get("forretningsadresse.postnummer") or profile.get("postal_code") or "").strip()
    target_city = str(reg_val.get("forretningsadresse.poststed") or profile.get("postal_area") or "").casefold()
    target_phone = _clean_phone(reg_val.get("telefon") or reg_val.get("mobil") or profile.get("phone"))

    website_val = (profile.get("evidence", {}).get("website", {}).get("value") or {})
    target_domain = _registered_domain(website_val.get("final_url") or profile.get("website"))

    subunits = (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations", [])
    subunit_orgs = {_clean_digits(s.get("organisasjonsnummer") or s.get("organisation_number")) for s in subunits}
    subunit_orgs.discard("")
    subunit_names = [str(s.get("navn") or s.get("name") or "").casefold() for s in subunits]

    # Extract candidate signals
    candidate_name = str(candidate.get("name") or candidate.get("title") or candidate.get("company_name") or "").strip()
    candidate_tokens = [t for t in _tokens(candidate_name) if t not in GENERIC_NAME_TOKENS]
    candidate_text = str(candidate.get("text") or candidate.get("snippet") or candidate.get("description") or "").casefold()
    candidate_address = str(candidate.get("address") or candidate.get("location") or "").casefold()
    candidate_postcode = str(candidate.get("postal_code") or candidate.get("postcode") or "").strip()
    candidate_city = str(candidate.get("city") or candidate.get("poststed") or "").casefold()
    candidate_phone = _clean_phone(candidate.get("phone") or candidate.get("telefon"))
    candidate_domain = _registered_domain(candidate.get("url") or candidate.get("web_site") or candidate.get("domain"))
    candidate_parent_org = _clean_digits(candidate.get("parent_organisation_number"))

    matched_signals: list[str] = []
    rejected_signals: list[str] = []
    score = 0.0

    # 1. Negative Hard Rejection Gates
    if candidate_org and target_org and candidate_org != target_org:
        # Check if candidate_org is a registered subunit of target
        if candidate_org in subunit_orgs:
            matched_signals.append(f"candidate_org_is_registered_subunit:{candidate_org}")
            score += 0.8
        else:
            rejected_signals.append(f"conflicting_organisation_number:{candidate_org}!={target_org}")
            return EntityResolutionResult(
                candidate_id=candidate_id,
                match_state="rejected",
                confidence=0.0,
                publishable=False,
                matched_signals=matched_signals,
                rejected_signals=rejected_signals,
                explanation=f"Hard rejection: candidate organisation number '{candidate_org}' belongs to a different legal entity.",
                details={"target_org": target_org, "candidate_org": candidate_org},
            )

    # 2. Organisation Number Exact Match
    org_in_text = bool(target_org and (target_org in _clean_digits(candidate_text) or target_org in _clean_digits(candidate_address)))
    if candidate_org == target_org and target_org:
        matched_signals.append("exact_organisation_number_match")
        score += 1.0
    elif org_in_text:
        matched_signals.append("organisation_number_found_in_candidate_evidence")
        score += 0.95

    # 3. Domain Match
    if target_domain and candidate_domain:
        if target_domain == candidate_domain or candidate_domain.endswith("." + target_domain):
            matched_signals.append(f"verified_domain_match:{candidate_domain}")
            score += 0.85
        else:
            rejected_signals.append(f"domain_mismatch:{candidate_domain}!={target_domain}")

    # 4. Name Matching
    name_overlap = set(target_tokens) & set(candidate_tokens)
    ratio = len(name_overlap) / len(target_tokens) if target_tokens else 0.0
    exact_name = bool(target_tokens and set(target_tokens).issubset(set(candidate_tokens)))

    if exact_name:
        matched_signals.append("exact_distinctive_name_tokens_match")
        score += 0.5
    elif ratio >= 0.75 and len(name_overlap) >= 2:
        matched_signals.append(f"high_token_name_overlap:{ratio:.2f}")
        score += 0.35
    elif ratio >= 0.5:
        matched_signals.append(f"partial_name_overlap:{ratio:.2f}")
        score += 0.15
    else:
        rejected_signals.append("name_tokens_mismatch")

    # 5. Address & Location Match
    address_matched = False
    if target_postcode and (target_postcode == candidate_postcode or target_postcode in candidate_address):
        matched_signals.append(f"exact_postcode_match:{target_postcode}")
        score += 0.35
        address_matched = True

    if target_city and (target_city == candidate_city or target_city in candidate_address):
        matched_signals.append(f"city_match:{target_city}")
        score += 0.2

    if target_street:
        street_tokens = [t for t in _tokens(target_street) if len(t) >= 3]
        if street_tokens and all(st in candidate_address for st in street_tokens):
            matched_signals.append("exact_street_address_match")
            score += 0.5
            address_matched = True

    # 6. Phone Number Match
    if target_phone and candidate_phone and target_phone == candidate_phone:
        matched_signals.append(f"exact_phone_match:{target_phone}")
        score += 0.7

    # 7. Subunit Relationship
    if any(s_name in candidate_name.casefold() for s_name in subunit_names if len(s_name) > 5):
        matched_signals.append("matches_registered_subunit_name")
        score += 0.6

    # 8. Parent Company Relationship
    if candidate_parent_org and candidate_parent_org == target_org:
        matched_signals.append("candidate_declares_target_as_parent")
        score += 0.65

    # Assess State and Confidence
    # Rule of Truth: Never verify on name similarity alone!
    corroborating_signals = [s for s in matched_signals if not s.startswith("exact_distinctive_name") and not s.startswith("high_token")]
    has_strong_corroboration = any(
        s.startswith("exact_organisation_number")
        or s.startswith("organisation_number_found")
        or s.startswith("verified_domain_match")
        or s.startswith("exact_phone_match")
        or (s.startswith("exact_street") and address_matched)
        or s.startswith("candidate_org_is_registered_subunit")
        for s in matched_signals
    )

    if (exact_name or ratio >= 0.75) and has_strong_corroboration:
        state = "verified_match"
        confidence = min(1.0, round(0.90 + score * 0.05, 3))
        publishable = True
        explanation = f"Verified entity match via legal name corroborated by {', '.join(corroborating_signals)}."
    elif score >= 1.0 and has_strong_corroboration:
        state = "verified_match"
        confidence = min(1.0, round(0.90 + (score - 1.0) * 0.05, 3))
        publishable = True
        explanation = f"Verified entity anchor via authoritative identifier(s): {', '.join(matched_signals)}."
    elif target_domain and candidate_domain and target_domain == candidate_domain:
        state = "probable_match"
        confidence = 0.85
        publishable = True
        explanation = f"Probable match: corroborated by verified domain '{target_domain}', though exact legal org number was absent on candidate page."
    elif exact_name and not has_strong_corroboration:
        # NAME-ONLY FALSE POSITIVE GATE
        state = "ambiguous"
        confidence = 0.50
        publishable = False
        rejected_signals.append("name_only_unsupported_by_address_phone_or_domain")
        explanation = "Ambiguous candidate: matching company name observed without external corroboration (no verified org number, address, phone, or domain)."
    elif ratio >= 0.5:
        state = "ambiguous"
        confidence = 0.40
        publishable = False
        explanation = "Ambiguous candidate: partial name similarity only."
    else:
        state = "rejected"
        confidence = 0.10
        publishable = False
        explanation = "Rejected candidate: insufficient entity identity evidence."

    return EntityResolutionResult(
        candidate_id=candidate_id,
        match_state=state,
        confidence=confidence,
        publishable=publishable,
        matched_signals=matched_signals,
        rejected_signals=rejected_signals,
        explanation=explanation,
        details={
            "score": round(score, 3),
            "target_org": target_org,
            "target_name": target_name,
            "candidate_name": candidate_name,
        },
    )
