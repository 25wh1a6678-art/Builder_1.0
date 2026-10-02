#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.synthesis import synthesize_company_profile  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from norway_company_agent.workforce import research_workforce_and_jobs  # noqa: E402
from norway_company_agent.footprint import research_external_footprint  # noqa: E402
from norway_company_agent.planner import BatchPlannerTelemetry, plan_company_research  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,workforce,external_footprint,adaptive_plan,synthesis")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "workforce", "external_footprint", "adaptive_plan", "synthesis"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}
    planner_telemetry = BatchPlannerTelemetry()

    def enrich(profile: dict) -> tuple[dict, dict]:
        # 1. Adaptive Planning
        plan = plan_company_research(profile)
        planner_telemetry.record_plan(plan)
        profile["adaptive_plan"] = plan.to_dict()
        profile["evidence"]["adaptive_plan"] = evidence(
            field="adaptive_plan",
            status="available",
            source_type="rule_based_adaptive_planner",
            source_url="internal://engine/planner/v4",
            value=plan.to_dict(),
            content_sha256=hashlib.sha256(json.dumps(plan.to_dict(), sort_keys=True).encode()).hexdigest(),
            retrieved_at=utc_now(),
        )

        # 2. Official Modules with Adaptive Group Filtering
        effective_fetch = fetch_modules
        if not plan.should_execute("group") and "group" in effective_fetch:
            effective_fetch = effective_fetch - {"group"}
        records, metrics = fetch_official_modules(profile["organisation_number"], effective_fetch)
        profile["evidence"].update(records)

        # 3. Website with Adaptive Crawl Decision
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules and plan.should_execute("website_crawl"):
            website_record, website_metrics = fetch_website(profile.get("website"))
            profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
        elif "website" in requested_modules:
            profile["evidence"]["website"] = evidence(
                "website", "not_found", "registry_linked_company_website",
                "https://data.brreg.no/enhetsregisteret/api/enheter",
                note=plan.skip_reasons.get("website_crawl", "Skipped by adaptive planner")
            )

        # 4. Workforce with Adaptive Plan
        workforce_metrics = {"requests": 0}
        if "workforce" in requested_modules:
            wf_record, workforce_metrics = research_workforce_and_jobs(profile, plan=plan)
            profile["evidence"]["workforce"] = wf_record

        # 5. External Footprint & Reviews with Adaptive Plan
        footprint_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "external_footprint" in requested_modules:
            fp_record, footprint_metrics = research_external_footprint(profile, plan=plan)
            profile["evidence"]["external_footprint"] = fp_record

        # 6. Synthesis Layer
        if "synthesis" in requested_modules:
            profile = synthesize_company_profile(profile)

        total_reqs = len(metrics) + website_metrics["requests"] + workforce_metrics.get("requests", 0) + footprint_metrics.get("requests", 0)
        cache_hits = 1 if (workforce_metrics.get("requests") == 0 and plan.should_execute("workforce_external_jobs")) else 0
        useful_obs = len((profile.get("external_footprint") or {}).get("all_accepted_observations", []))
        planner_telemetry.record_run_metrics(total_reqs, cache_hits, useful_obs)

        metric = {
            "requests": total_reqs,
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"] + footprint_metrics.get("bytes", 0),
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"] + footprint_metrics.get("latencies_ms", []),
        }
        profile["run_metrics"] = metric
        return profile, metric



    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "planner_telemetry": planner_telemetry.summary(),
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
