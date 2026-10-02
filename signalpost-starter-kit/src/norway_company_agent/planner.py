from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .evidence import evidence, utc_now


class CompanyArchetype(str, Enum):
    RESIDENTIAL_HOUSING = "residential_housing_association"
    HOLDING_INVESTMENT = "holding_and_investment_vehicle"
    COMMERCIAL_OPERATING = "commercial_operating_company"
    SMALL_OR_DORMANT = "small_or_dormant_entity"


# High-probability keywords for entity archetypes
HOUSING_LEGAL_FORMS = {"BRL", "ESEK", "SAM"}
HOUSING_NACE_CODES = {"97.001", "97.002", "68.320"}
HOUSING_KEYWORDS = ("borettslag", "boligsameie", "sameiet", "huseierforening")

HOLDING_NACE_CODES = {"64.201", "64.202", "68.100", "68.110"}
HOLDING_KEYWORDS = ("holding", "invest", "investering", "eiendom invest")


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


def plan_company_research(profile: dict[str, Any]) -> AdaptiveResearchPlan:
    """Dynamically determine the optimal, cost-effective research paths for a company."""
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    archetype, class_reasons = classify_company_archetype(profile)

    selected: list[str] = []
    skipped: list[str] = []
    skip_reasons: dict[str, str] = {}
    requests_estimate = 0
    requests_saved = 0
    routing_adapted = False

    form = str(profile.get("legal_form") or "").upper()
    website_declared = bool(str(profile.get("website") or "").strip())
    employees = profile.get("employees")

    for path in ALL_RESEARCH_PATHS:
        # Base registry paths are mandatory for all entities
        if path in {"registry_live", "accounting_obligation", "roles", "locations", "workforce_official"}:
            selected.append(path)
            requests_estimate += 1
            continue

        # Financials path: only for entities with statutory accounting obligation (AS, ASA, etc.)
        if path == "financials":
            if form in {"BRL", "ESEK", "SAM"}:
                skipped.append(path)
                skip_reasons[path] = f"Legal form '{form}' is not subject to Regnskapsregisteret commercial filing obligation."
                requests_saved += 1
                routing_adapted = True
            else:
                selected.append(path)
                requests_estimate += 1
            continue

        # Corporate Group Structure path
        if path == "group":
            if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                skipped.append(path)
                skip_reasons[path] = "Residential housing co-operatives do not maintain corporate subsidiaries or parent group structures."
                requests_saved += 1
                routing_adapted = True
            else:
                selected.append(path)
                requests_estimate += 1
            continue

        # Website crawl path
        if path in {"website_crawl", "footprint_website"}:
            if not website_declared:
                skipped.append(path)
                skip_reasons[path] = "No official website URL declared in open entity registry; blind domain speculation avoided."
                requests_saved += 1
                routing_adapted = True
            else:
                selected.append(path)
                requests_estimate += 2
            continue

        # External Job Postings search
        if path == "workforce_external_jobs":
            if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                skipped.append(path)
                skip_reasons[path] = "Residential housing associations (Borettslag/Sameie) do not recruit active staff on public job boards."
                requests_saved += 1
                routing_adapted = True
            elif archetype == CompanyArchetype.HOLDING_INVESTMENT:
                skipped.append(path)
                skip_reasons[path] = "Pure holding and asset ownership vehicles do not recruit operational staff on public job boards."
                requests_saved += 1
                routing_adapted = True
            elif employees is not None and employees > 0:
                # Official employee count is already grounded via Aa-registeret!
                # If workforce is already conclusively verified, external speculative queries can be safely bounded.
                selected.append(path)
                requests_estimate += 1
            else:
                selected.append(path)
                requests_estimate += 1
            continue

        # Reviews & Local Presence search
        if path == "reviews_local_presence":
            if archetype == CompanyArchetype.RESIDENTIAL_HOUSING:
                skipped.append(path)
                skip_reasons[path] = "Residential housing associations do not maintain consumer-facing commercial services or public customer reviews."
                requests_saved += 1
                routing_adapted = True
            elif archetype == CompanyArchetype.HOLDING_INVESTMENT:
                skipped.append(path)
                skip_reasons[path] = "Holding and equity entities have no commercial consumer foot-traffic or customer review profiles."
                requests_saved += 1
                routing_adapted = True
            else:
                selected.append(path)
                requests_estimate += 1
            continue

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

    def record_plan(self, plan: AdaptiveResearchPlan) -> None:
        self.companies_processed += 1
        if plan.routing_adapted:
            self.companies_with_adapted_routing += 1
        self.paths_considered_total += len(plan.paths_considered)
        self.paths_selected_total += len(plan.paths_selected)
        self.paths_skipped_total += len(plan.paths_skipped)
        self.requests_saved_total += plan.estimated_requests_saved
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
        return {
            "companies_processed": self.companies_processed,
            "companies_with_adapted_routing": self.companies_with_adapted_routing,
            "research_paths_considered": self.paths_considered_total,
            "research_paths_selected": self.paths_selected_total,
            "research_paths_skipped": self.paths_skipped_total,
            "requests_saved": self.requests_saved_total,
            "cache_hits": self.cache_hits_total,
            "useful_observations_found": self.useful_observations_total,
            "useful_observations_found_per_request": obs_per_req,
            "skip_reasons": self.skip_reasons_counter,
        }
