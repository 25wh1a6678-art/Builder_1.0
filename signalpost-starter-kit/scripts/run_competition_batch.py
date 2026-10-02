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
from norway_company_agent.official import fetch_official_modules, serve_snapshot_registry_live  # noqa: E402
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
    operations = {"requests": 0, "bytes": 0, "latencies_ms": [], "status_counts": {}}
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

        # 2. Official Modules with Tier 1 Snapshot Baseline & Adaptive Expected-Yield Routing
        effective_fetch = set(fetch_modules)
        if "registry_live" in effective_fetch:
            if not plan.should_execute("registry_live"):
                effective_fetch.remove("registry_live")
                profile["evidence"]["registry_live"] = serve_snapshot_registry_live(profile)

        for m in ("financials", "roles", "locations", "group"):
            if m in effective_fetch and not plan.should_execute(m):
                effective_fetch.remove(m)
                profile["evidence"][m] = evidence(
                    m,
                    "not_applicable",
                    f"official_{m}",
                    f"https://data.brreg.no/enhetsregisteret/api/{m}",
                    note=plan.skip_reasons.get(m, f"Skipped by adaptive expected-yield planner for archetype {plan.archetype.value}"),
                )

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

        status_counts: dict[str, int] = {}
        for m in metrics:
            c = str(m.status)
            status_counts[c] = status_counts.get(c, 0) + 1

        total_reqs = len(metrics) + website_metrics["requests"] + workforce_metrics.get("requests", 0) + footprint_metrics.get("requests", 0)
        cache_hits = 1 if (workforce_metrics.get("requests") == 0 and plan.should_execute("workforce_external_jobs")) else 0
        useful_obs = len((profile.get("external_footprint") or {}).get("all_accepted_observations", []))
        planner_telemetry.record_run_metrics(total_reqs, cache_hits, useful_obs)

        metric = {
            "requests": total_reqs,
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"] + footprint_metrics.get("bytes", 0),
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"] + footprint_metrics.get("latencies_ms", []),
            "status_counts": status_counts,
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
            for code, cnt in metric.get("status_counts", {}).items():
                operations["status_counts"][code] = operations["status_counts"].get(code, 0) + cnt
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

    # Calculate throughput and budget metrics
    from datetime import datetime
    t_start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    t_end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    runtime_sec = max(0.001, (t_end - t_start).total_seconds())
    companies_per_min = round((len(envelopes) / runtime_sec) * 60, 2)
    reqs_per_co = round(operations["requests"] / max(1, len(envelopes)), 2)

    # Aggregate scaling telemetry across profiles
    total_obs = 0
    total_claims = 0
    total_rejected = 0
    total_ambiguous = 0
    total_wrong_company = 0
    total_conflicts = 0
    total_changes = 0

    for p in ordered_profiles:
        fp = p.get("external_footprint") or {}
        obs = fp.get("all_accepted_observations", [])
        total_obs += len(obs)

        claims = p.get("grounded_claims") or []
        total_claims += len(claims)

        rej_fp = fp.get("all_rejected_observations", [])
        total_rejected += len(rej_fp)
        for r in rej_fp:
            exp = str(r.get("reasons", "")).lower()
            if "different legal entity" in exp or "conflicting_organisation_number" in exp:
                total_wrong_company += 1
            elif "ambiguous" in exp:
                total_ambiguous += 1

        wf_ev = p.get("evidence", {}).get("workforce", {})
        if wf_ev.get("value"):
            wf_val = wf_ev["value"]
            wf_rej = wf_val.get("rejected_candidates", 0)
            total_rejected += wf_rej

        tprof = p.get("temporal_profile") or {}
        changes = tprof.get("detected_changes", [])
        total_changes += len(changes)

        fhist = tprof.get("fact_histories", {})
        for f, fh in fhist.items():
            if isinstance(fh, dict):
                if fh.get("temporal_status") == "conflicting_current":
                    total_conflicts += 1
                for alt in fh.get("rejected_alternatives", []):
                    reason = str(alt.get("reason", "")).lower()
                    if "conflicting" in reason:
                        total_conflicts += 1
                    if "entity" in reason or "wrong" in reason:
                        total_wrong_company += 1

    status_counts = operations.get("status_counts", {})
    s429 = status_counts.get("429", 0)
    s403 = status_counts.get("403", 0)
    s503 = status_counts.get("503", 0)
    known_statuses = {"200", "404", "410", "429", "403", "503"}
    other_errs = sum(cnt for code, cnt in status_counts.items() if code not in known_statuses)

    cache_hits = planner_telemetry.cache_hits_total
    cache_misses = max(0, operations["requests"] - cache_hits)

    scaling_summary = {
        "company_count": len(envelopes),
        "completed_count": len(envelopes),
        "failed_count": 0,
        "terminal_envelopes": len(envelopes),
        "silent_drops": args.expected_count - len(envelopes),
        "http_requests": operations["requests"],
        "requests_per_company": reqs_per_co,
        "runtime_seconds": round(runtime_sec, 2),
        "companies_per_minute": companies_per_min,
        "p50_latency_ms": operations.get("p50_ms"),
        "p95_latency_ms": operations.get("p95_ms"),
        "status_429_count": s429,
        "status_403_count": s403,
        "status_503_count": s503,
        "other_errors_count": other_errs,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "external_observations": total_obs,
        "grounded_claims": total_claims,
        "rejected_candidates": total_rejected,
        "ambiguous_candidates": total_ambiguous,
        "wrong_company_candidates": total_wrong_company,
        "conflicts": total_conflicts,
        "temporal_changes": total_changes,
    }

    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "runtime_seconds": round(runtime_sec, 2),
        "companies_per_minute": companies_per_min,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "requests_per_company": reqs_per_co,
        "requests_remaining_budget": max(0, 2000 - operations["requests"]),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "planner_telemetry": planner_telemetry.summary(),
        "scaling_summary": scaling_summary,
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
