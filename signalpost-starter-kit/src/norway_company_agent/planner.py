from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any

from .conflict_resolution import SOURCE_AUTHORITY_RANK
from .evidence import evidence, utc_now


class CompanyArchetype(str, Enum):
    RESIDENTIAL_HOUSING = "residential_housing_association"
    HOLDING_INVESTMENT = "holding_and_investment_vehicle"
    COMMERCIAL_OPERATING = "commercial_operating_company"
    SMALL_OR_DORMANT = "small_or_dormant_entity"


class ResearchTargetCategory(str, Enum):
    CORE_OFFICIAL = "core_official"
    EXTERNAL_OBSERVATION = "external_observation"


# High-probability keywords for entity archetypes
HOUSING_LEGAL_FORMS = {"BRL", "ESEK", "SAM"}
HOUSING_NACE_CODES = {"97.001", "97.002", "68.320"}
HOUSING_KEYWORDS = ("borettslag", "boligsameie", "sameiet", "huseierforening")

HOLDING_NACE_CODES = {"64.201", "64.202", "68.100", "68.110"}
HOLDING_KEYWORDS = ("holding", "invest", "investering", "eiendom invest")

# Configurable per-archetype outbound request budgets
ARCHETYPE_BUDGETS: dict[CompanyArchetype, int] = {
    CompanyArchetype.COMMERCIAL_OPERATING: 10,
    CompanyArchetype.HOLDING_INVESTMENT: 4,
    CompanyArchetype.RESIDENTIAL_HOUSING: 3,
    CompanyArchetype.SMALL_OR_DORMANT: 4,
}

# Core official vs external target categories
CORE_TARGET_FIELDS = [
    "identity",
    "legal_form",
    "address",
    "financials",
    "roles",
    "statutory_workforce",
]

EXTERNAL_TARGET_FIELDS = [
    "official_website_footprint",
    "public_profile_observations",
    "job_workforce_signals",
    "reviews_local_presence",
    "public_facing_business_descriptions",
]

ALL_RESEARCH_PATHS = [
    "registry_live",
    "accounting_obligation",
    "financials",
    "roles",
    "group",
    "locations",
    "website_crawl",
    "workforce_official",
    "workforce_external_jobs",
    "footprint_website",
    "reviews_local_presence",
]

PATH_SOURCE_TYPE_MAP = {
    "registry_live": "official_registry_live",
    "accounting_obligation": "official_registry_live",
    "financials": "official_financials",
    "roles": "official_roles",
    "group": "official_api",
    "locations": "official_subunits",
    "website_crawl": "company_owned",
    "workforce_official": "official_registry_bulk",
    "workforce_external_jobs": "job_board",
    "footprint_website": "company_site",
    "reviews_local_presence": "review_platform",
}


@dataclass
class CompanyResearchState:
    organisation_number: str
    company_archetype: str
    budget_total: int
    budget_used: int = 0
    budget_remaining: int = 0
    fields_already_supported: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    fields_low_confidence: list[str] = field(default_factory=list)
    fields_unresolved_conflicts: list[str] = field(default_factory=list)
    fields_not_applicable: list[str] = field(default_factory=list)
    external_observations_found: int = 0
    external_observations_missing: list[str] = field(default_factory=list)
    sources_already_queried: list[str] = field(default_factory=list)
    expected_yields: dict[str, float] = field(default_factory=dict)
    diminishing_returns: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RequestDeduplicator:
    """Track executed requests, URLs, and source outcomes to prevent redundant HTTP queries."""

    def __init__(self) -> None:
        self.executed_urls: set[str] = set()
        self.blocked_urls: set[str] = set()
        self.not_applicable_urls: set[str] = set()
        self.cached_responses: dict[str, Any] = {}
        self.deduplicated_count: int = 0

    def is_redundant(self, url: str) -> bool:
        norm = url.strip().rstrip("/").casefold()
        if norm in self.executed_urls or norm in self.blocked_urls or norm in self.not_applicable_urls:
            self.deduplicated_count += 1
            return True
        return False

    def is_blocked(self, url: str) -> bool:
        norm = url.strip().rstrip("/").casefold()
        return norm in self.blocked_urls

    def is_not_applicable(self, url: str) -> bool:
        norm = url.strip().rstrip("/").casefold()
        return norm in self.not_applicable_urls

    def record_outcome(self, url: str, status: str, response: Any = None) -> None:
        norm = url.strip().rstrip("/").casefold()
        self.executed_urls.add(norm)
        if status == "blocked":
            self.blocked_urls.add(norm)
        elif status == "not_applicable":
            self.not_applicable_urls.add(norm)
        if response is not None:
            self.cached_responses[norm] = response


# Global process-level deduplicator singleton
GLOBAL_DEDUPLICATOR = RequestDeduplicator()


def calculate_expected_yield(
    path: str,
    archetype: CompanyArchetype,
    has_website: bool,
    is_commercial: bool,
    field_covered: bool,
    field_not_applicable: bool,
    consecutive_zero_yields: int = 0,
    estimated_cost: int = 1,
    has_accounts: bool = False,
) -> float:
    """Calculate deterministic expected yield score = (prob_verified * info_value * authority) / cost."""
    if field_not_applicable:
        return 0.0

    stype = PATH_SOURCE_TYPE_MAP.get(path, "permitted_public_page")
    authority = SOURCE_AUTHORITY_RANK.get(stype, 0.70)
    info_value = 1.0 if not field_covered else 0.20

    # Probability of obtaining a new verified observation
    if path in {"registry_live", "accounting_obligation", "roles", "locations", "workforce_official"}:
        prob_success = 0.95
    elif path == "financials":
        if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
            prob_success = 0.90 if has_accounts else 0.0
        else:
            prob_success = 0.90
    elif path == "group":
        prob_success = 0.60 if archetype == CompanyArchetype.COMMERCIAL_OPERATING else 0.10
    elif path in {"website_crawl", "footprint_website"}:
        prob_success = 0.88 if has_website else 0.05
    elif path == "workforce_external_jobs":
        if archetype == CompanyArchetype.COMMERCIAL_OPERATING and is_commercial:
            prob_success = 0.65
        elif archetype == CompanyArchetype.SMALL_OR_DORMANT:
            prob_success = 0.20
        else:
            prob_success = 0.05
    elif path == "reviews_local_presence":
        if archetype == CompanyArchetype.COMMERCIAL_OPERATING and has_website:
            prob_success = 0.55
        else:
            prob_success = 0.05
    else:
        prob_success = 0.30

    # Diminishing returns penalty
    if consecutive_zero_yields > 0:
        diminishing_factor = max(0.20, 1.0 - (0.30 * consecutive_zero_yields))
        prob_success *= diminishing_factor

    score = (prob_success * info_value * authority) / max(1, estimated_cost)
    return round(score, 4)


def should_early_stop(state: CompanyResearchState) -> tuple[bool, str | None]:
    """Determine whether research can be terminated early under V8 rules."""
    if state.budget_remaining <= 0:
        return True, "Research budget exhausted."

    # Condition A: Core required fields must be covered
    core_required = {"identity", "legal_form", "address"}
    core_missing = [f for f in core_required if f in state.fields_missing]
    if core_missing:
        return False, None

    # Condition B: Check applicable external targets
    applicable_external_remaining = [
        target for target in state.external_observations_missing
        if target not in state.fields_not_applicable
        and state.expected_yields.get(target, 0.0) >= 0.20
    ]
    if applicable_external_remaining:
        return False, None

    return True, "Core targets satisfied and applicable external targets either resolved, not applicable, or low expected yield."


@dataclass
class AdaptiveResearchPlan:
    organisation_number: str
    company_name: str
    archetype: CompanyArchetype
    classification_reasons: list[str]
    paths_considered: list[str] = field(default_factory=lambda: list(ALL_RESEARCH_PATHS))
    paths_selected: list[str] = field(default_factory=list)
    paths_skipped: list[str] = field(default_factory=list)
    skip_reasons: dict[str, str] = field(default_factory=dict)
    estimated_requests: int = 0
    estimated_requests_saved: int = 0
    routing_adapted: bool = False
    expected_yields: dict[str, float] = field(default_factory=dict)
    budget_total: int = 10
    budget_used: int = 0
    budget_remaining: int = 10
    early_stopped: bool = False
    early_stop_reason: str | None = None
    deduplicated_requests: int = 0
    research_state: dict[str, Any] = field(default_factory=dict)

    def should_execute(self, path: str) -> bool:
        return path in self.paths_selected

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["archetype"] = self.archetype.value
        return data


def classify_company_archetype(profile: dict[str, Any]) -> tuple[CompanyArchetype, list[str]]:
    """Classify company into an operational archetype using anchored BRREG registry signals."""
    reasons = []
    name = str(profile.get("name") or "").casefold()
    form = str(profile.get("legal_form") or "").upper()
    nace = str(profile.get("industry_code") or "").strip()

    # 1. Residential Housing Associations (Borettslag, Eierseksjonssameie)
    if form in HOUSING_LEGAL_FORMS or nace in HOUSING_NACE_CODES or any(k in name for k in HOUSING_KEYWORDS):
        reasons.append(f"Legal form '{form}' / NACE '{nace}' / name matches residential housing cooperative (Borettslag/Sameie).")
        return CompanyArchetype.RESIDENTIAL_HOUSING, reasons

    # 2. Pure Holding / Investment Vehicles
    if nace in HOLDING_NACE_CODES or any(k in name for k in HOLDING_KEYWORDS):
        reasons.append(f"NACE '{nace}' / name contains holding/investment indicators without active consumer service operations.")
        return CompanyArchetype.HOLDING_INVESTMENT, reasons

    # 3. Commercial Operating Company
    has_website = bool(str(profile.get("website") or "").strip())
    employees = profile.get("employees")
    if has_website or (employees is not None and employees > 0):
        reasons.append(f"Entity possesses active commercial operations (employees: {employees}, website declared: {has_website}).")
        return CompanyArchetype.COMMERCIAL_OPERATING, reasons

    # 4. Small or Dormant / Default Operating AS
    reasons.append("Standard private limited entity (AS) without declared commercial website or recorded employees in snapshot.")
    return CompanyArchetype.SMALL_OR_DORMANT, reasons


def plan_company_research(
    profile: dict[str, Any],
    deduplicator: RequestDeduplicator | None = None,
    consecutive_diminishing_returns: dict[str, int] | None = None,
    use_bulk_baseline: bool | None = None,
) -> AdaptiveResearchPlan:
    """Dynamically determine the optimal, expected-yield routed research paths for a company."""
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    archetype, class_reasons = classify_company_archetype(profile)

    dedup = deduplicator or GLOBAL_DEDUPLICATOR
    diminishing = consecutive_diminishing_returns or {}

    form = str(profile.get("legal_form") or "").upper()
    website_url = str(profile.get("website") or "").strip()
    website_declared = bool(website_url)
    employees = profile.get("employees")
    is_commercial = bool((employees is not None and employees > 0) or website_declared or form == "ASA")

    # Archetype budget
    base_budget = ARCHETYPE_BUDGETS.get(archetype, 4)
    if website_declared:
        budget_total = max(base_budget, 10)
    else:
        budget_total = base_budget

    selected: list[str] = []
    skipped: list[str] = []
    skip_reasons: dict[str, str] = {}
    expected_yields: dict[str, float] = {}
    requests_estimate = 0
    requests_saved = 0
    routing_adapted = False
    deduplicated = 0

    # Initial state tracking
    fields_supported: list[str] = ["identity", "legal_form", "address"]
    fields_missing: list[str] = []
    fields_na: list[str] = []
    ext_missing: list[str] = list(EXTERNAL_TARGET_FIELDS)

    # Check if bulk baseline is active (e.g. from profiles_from_bulk)
    if use_bulk_baseline is None:
        use_bulk_baseline = bool(profile.get("evidence", {}).get("registry")) or bool(profile.get("raw")) or bool(profile.get("use_bulk_baseline", False))

    for path in ALL_RESEARCH_PATHS:
        cost = 2 if path in {"website_crawl", "footprint_website"} else 1
        dim_count = diminishing.get(path, 0)

        # Deduplication check
        if path == "website_crawl" and website_url and dedup.is_blocked(website_url):
            skipped.append(path)
            skip_reasons[path] = f"Source URL '{website_url}' previously confirmed blocked (robots/HTTP)."
            requests_saved += cost
            deduplicated += cost
            routing_adapted = True
            expected_yields[path] = 0.0
            continue

        if path == "registry_live":
            if use_bulk_baseline:
                skipped.append(path)
                skip_reasons[path] = "Core statutory entity facts fully satisfied by local statutory registry snapshot; redundant live HTTP call avoided."
                requests_saved += cost
                routing_adapted = True
                fields_supported.append("registry_live")
                expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

        if path in {"accounting_obligation", "workforce_official"}:
            selected.append(path)
            requests_estimate += 0
            ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, 1)
            expected_yields[path] = ey
            continue

        if path == "roles":
            if use_bulk_baseline:
                if archetype in {CompanyArchetype.COMMERCIAL_OPERATING, CompanyArchetype.HOLDING_INVESTMENT, CompanyArchetype.RESIDENTIAL_HOUSING} or (employees is not None and employees > 0) or website_declared:
                    selected.append(path)
                    requests_estimate += cost
                    ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                    expected_yields[path] = ey
                else:
                    skipped.append(path)
                    skip_reasons[path] = "Small or dormant entity without active workforce or commercial website; governance roles query deferred."
                    requests_saved += cost
                    routing_adapted = True
                    fields_na.append("governance_roles")
                    expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

        if path == "locations":
            if use_bulk_baseline:
                if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                    skipped.append(path)
                    skip_reasons[path] = "Residential housing associations do not maintain commercial operating subunits."
                    requests_saved += cost
                    routing_adapted = True
                    fields_na.append("subunit_locations")
                    expected_yields[path] = 0.0
                elif archetype in {CompanyArchetype.HOLDING_INVESTMENT, CompanyArchetype.SMALL_OR_DORMANT}:
                    skipped.append(path)
                    skip_reasons[path] = "Entity without registered active employees does not maintain distinct operational subunit locations."
                    requests_saved += cost
                    routing_adapted = True
                    fields_na.append("subunit_locations")
                    expected_yields[path] = 0.0
                else:
                    reg_val = profile.get("evidence", {}).get("registry", {}).get("value") or profile.get("raw") or {}
                    har_ansatte = str(reg_val.get("harRegistrertAntallAnsatte") or "").lower() == "true"
                    has_active_workforce = (employees is not None and employees > 0) or har_ansatte
                    if has_active_workforce:
                        selected.append(path)
                        requests_estimate += cost
                        ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                        expected_yields[path] = ey
                    else:
                        skipped.append(path)
                        skip_reasons[path] = "Entity without registered active employees or operational workplaces in snapshot; subunit query deferred."
                        requests_saved += cost
                        routing_adapted = True
                        fields_na.append("subunit_locations")
                        expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

        if path == "financials":
            reg_val = profile.get("evidence", {}).get("registry", {}).get("value")
            latest_accounts = (
                profile.get("latest_submitted_accounts")
                or (reg_val.get("sisteInnsendteAarsregnskap") if isinstance(reg_val, dict) else None)
                or (profile.get("raw", {}).get("sisteInnsendteAarsregnskap") if isinstance(profile.get("raw"), dict) else None)
            )
            has_accounts = bool(latest_accounts)
            if form in {"BRL", "ESEK", "SAM"} and not has_accounts:
                skipped.append(path)
                skip_reasons[path] = f"Legal form '{form}' without submitted accounts; Regnskapsregisteret commercial filing obligation not applicable."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("financials")
                expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost, has_accounts=has_accounts)
                expected_yields[path] = ey
            continue

        if path == "group":
            if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                skipped.append(path)
                skip_reasons[path] = "Residential housing co-operatives do not maintain corporate subsidiaries or parent group structures."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("group_structure")
                expected_yields[path] = 0.0
            elif use_bulk_baseline:
                raw = profile.get("evidence", {}).get("registry", {}).get("value") or profile.get("raw") or {}
                in_group = str(raw.get("erIKonsern") or "").lower() == "ja" or bool(raw.get("overordnetEnhet"))
                if in_group:
                    selected.append(path)
                    requests_estimate += cost
                    ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                    expected_yields[path] = ey
                else:
                    skipped.append(path)
                    skip_reasons[path] = "Statutory snapshot reports entity is not part of a corporate group (erIKonsern!=Ja, no parent entity); redundant 404 query avoided."
                    requests_saved += cost
                    routing_adapted = True
                    fields_na.append("group_structure")
                    expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

        if path in {"website_crawl", "footprint_website"}:
            if not website_declared:
                skipped.append(path)
                skip_reasons[path] = "No official website URL declared in open entity registry; blind domain speculation avoided."
                requests_saved += cost
                routing_adapted = True
                fields_na.append(path)
                expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

        if path == "workforce_external_jobs":
            if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                skipped.append(path)
                skip_reasons[path] = "Residential housing associations (Borettslag/Sameie) do not recruit active staff on public job boards."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("hiring_activity")
                expected_yields[path] = 0.0
            elif archetype == CompanyArchetype.HOLDING_INVESTMENT:
                skipped.append(path)
                skip_reasons[path] = "Pure holding and asset ownership vehicles do not recruit operational staff on public job boards."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("hiring_activity")
                expected_yields[path] = 0.0
            elif use_bulk_baseline and archetype == CompanyArchetype.SMALL_OR_DORMANT and not is_commercial:
                skipped.append(path)
                skip_reasons[path] = "Small or dormant entity with zero registered employees does not recruit operational staff on public job boards."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("hiring_activity")
                expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

        if path == "reviews_local_presence":
            if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                skipped.append(path)
                skip_reasons[path] = "Residential housing associations do not maintain consumer-facing commercial services or public customer reviews."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("customer_reviews_sentiment")
                expected_yields[path] = 0.0
            elif archetype == CompanyArchetype.HOLDING_INVESTMENT:
                skipped.append(path)
                skip_reasons[path] = "Holding and equity entities have no commercial consumer foot-traffic or customer review profiles."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("customer_reviews_sentiment")
                expected_yields[path] = 0.0
            elif use_bulk_baseline and archetype == CompanyArchetype.SMALL_OR_DORMANT and not is_commercial:
                skipped.append(path)
                skip_reasons[path] = "Small or dormant entity without active consumer-facing operations has no commercial customer review profiles."
                requests_saved += cost
                routing_adapted = True
                fields_na.append("customer_reviews_sentiment")
                expected_yields[path] = 0.0
            else:
                selected.append(path)
                requests_estimate += cost
                ey = calculate_expected_yield(path, archetype, website_declared, is_commercial, False, False, dim_count, cost)
                expected_yields[path] = ey
            continue

    # Order selected paths by expected yield descending
    selected.sort(key=lambda p: expected_yields.get(p, 0.0), reverse=True)

    budget_remaining = max(0, budget_total - requests_estimate)
    research_state = CompanyResearchState(
        organisation_number=org,
        company_archetype=archetype.value,
        budget_total=budget_total,
        budget_used=requests_estimate,
        budget_remaining=budget_remaining,
        fields_already_supported=fields_supported,
        fields_missing=fields_missing,
        fields_not_applicable=fields_na,
        external_observations_found=1 if website_declared else 0,
        external_observations_missing=ext_missing,
        expected_yields=expected_yields,
        diminishing_returns=diminishing,
    )

    can_stop, stop_reason = should_early_stop(research_state)

    return AdaptiveResearchPlan(
        organisation_number=org,
        company_name=name,
        archetype=archetype,
        classification_reasons=class_reasons,
        paths_selected=selected,
        paths_skipped=skipped,
        skip_reasons=skip_reasons,
        estimated_requests=requests_estimate,
        estimated_requests_saved=requests_saved,
        routing_adapted=routing_adapted,
        expected_yields=expected_yields,
        budget_total=budget_total,
        budget_used=requests_estimate,
        budget_remaining=budget_remaining,
        early_stopped=can_stop,
        early_stop_reason=stop_reason,
        deduplicated_requests=deduplicated,
        research_state=research_state.to_dict(),
    )


class BatchPlannerTelemetry:
    """Accumulate and report batch-level research planner performance and efficiency metrics."""

    def __init__(self) -> None:
        self.companies_processed = 0
        self.companies_with_adapted_routing = 0
        self.paths_considered_total = 0
        self.paths_selected_total = 0
        self.paths_skipped_total = 0
        self.skip_reasons_counter: dict[str, int] = {}
        self.requests_saved_total = 0
        self.cache_hits_total = 0
        self.useful_observations_total = 0
        self.actual_requests_total = 0
        self.deduplicated_requests_total = 0
        self.early_stops_count = 0
        self.expected_yield_sum = 0.0

    def record_plan(self, plan: AdaptiveResearchPlan) -> None:
        self.companies_processed += 1
        if plan.routing_adapted:
            self.companies_with_adapted_routing += 1
        self.paths_considered_total += len(plan.paths_considered)
        self.paths_selected_total += len(plan.paths_selected)
        self.paths_skipped_total += len(plan.paths_skipped)
        self.requests_saved_total += plan.estimated_requests_saved
        self.deduplicated_requests_total += plan.deduplicated_requests
        if plan.early_stopped:
            self.early_stops_count += 1
        self.expected_yield_sum += sum(plan.expected_yields.values())

        for path, reason in plan.skip_reasons.items():
            key = f"{path}: {reason}"
            self.skip_reasons_counter[key] = self.skip_reasons_counter.get(key, 0) + 1

    def record_run_metrics(self, requests: int, cache_hits: int, useful_observations: int) -> None:
        self.actual_requests_total += requests
        self.cache_hits_total += cache_hits
        self.useful_observations_total += useful_observations

    def summary(self) -> dict[str, Any]:
        obs_per_req = (
            round(self.useful_observations_total / self.actual_requests_total, 4)
            if self.actual_requests_total > 0
            else 0.0
        )
        avg_ey = (
            round(self.expected_yield_sum / max(1, self.paths_considered_total), 4)
        )
        return {
            "companies_processed": self.companies_processed,
            "companies_with_adapted_routing": self.companies_with_adapted_routing,
            "research_paths_considered": self.paths_considered_total,
            "research_paths_selected": self.paths_selected_total,
            "research_paths_skipped": self.paths_skipped_total,
            "requests_saved": self.requests_saved_total,
            "deduplicated_requests": self.deduplicated_requests_total,
            "cache_hits": self.cache_hits_total,
            "useful_observations_found": self.useful_observations_total,
            "useful_observations_found_per_request": obs_per_req,
            "early_stops_count": self.early_stops_count,
            "average_expected_yield": avg_ey,
            "skip_reasons": self.skip_reasons_counter,
        }
