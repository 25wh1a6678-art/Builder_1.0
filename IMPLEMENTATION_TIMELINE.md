# Builderr Signalpost Agent: Implementation Timeline & Version Breakdown

This document tracks the precise development duration, commit history, scope of work, and verification metrics for each version of the **Norwegian Company Research Agent** from initial baseline (V0) through to the production-ready submission candidate (V10 and production hardening).

---

## 1. Executive Summary & Timeline Overview

* **Total Core Development Time (V0 &rarr; V10)**: **~3 hours 8 minutes** (187.9 minutes)
* **Total Hardening & Budget Optimization (Post-V10)**: **~2.5 hours**
* **Total Test Suite**: 190 automated unit tests (100% passing)
* **Final Scaling Performance (100-company benchmark)**:
  * Outbound HTTP Requests: **180** (1.80 req/company)
  * Projected 1,000-Company Cost: **1,800 requests** (200 requests safety headroom under the 2,000 ceiling)
  * Processing Throughput: **113.5 companies/min** (~8.8 minutes for 1,000 companies)
  * Evidence Recall & Quality: **751 grounded claims, 992 historical facts, 915 temporal changes, 0 wrong-company matches, 0 silent drops**.

```
  Start (V0)                                                                Core V10 Complete
  15:46:05 ───► V1 ───► V2 ───► V3 ───► V4 ───► V5 ───► V6 ───► V7 ───► V8 ───► V9 ───► 18:53:35
    │           │      │      │      │      │      │      │      │      │        │
  Baseline    Synth   Jobs  Reviews Plan   Entity Resol Temporal Qual  Budget  Scale    Bulk BRREG
   (0m)       (11m)  (17m)   (14m)  (20m)  (15m)  (17m)  (19m)   (9m)  (20m)    (45m)
```

---

## 2. Detailed Version Breakdown (V0 to V10)

| Version | Commit Hash | Start Time (IST) | End Time (IST) | Duration | Key Additions & Deliverables | Verification & Test Outcome |
| :--- | :---: | :---: | :---: | :---: | :--- | :--- |
| **V0: Foundation Baseline** | `b8d91e6` | — | 15:46:05 | **Baseline** | Initial repository setup, Brønnøysund live API client, base company profile schema, and test harness. | Smoke tests passing; establishes statutory baseline. |
| **V1: Research Synthesis Engine** | `c1e5a7e` | 15:46:05 | 15:57:29 | **11.4 min** | Multi-view synthesis module generating structured summaries, business descriptions, unknown-field explanations, and source-attributed fact lists. | Synthesis unit tests pass (10/10 mock entities validated). |
| **V2: External Workforce & Jobs** | `0748ad5` | 15:57:29 | 16:14:50 | **17.4 min** | Workforce research layer integrating public career pages, job listings, and employment structure verification without credential requirements. | Unit tests for workforce parsing & contract checks pass. |
| **V3: Footprint, Reviews & Locations** | `4d1a3a0` | 16:14:50 | 16:29:02 | **14.2 min** | Web crawl extraction, contact/location discovery, and local review footprint connector (`fagfolkguiden.no`). | Extracted multi-page site signals & review footprints. |
| **V4: Adaptive Research Planner** | `530d9d7` | 16:29:02 | 16:49:04 | **20.0 min** | Company archetype classifier (Commercial, Holding, Housing, Inactive) with dynamic routing to eliminate redundant requests on dormant entities. | Routing telemetry verified; skips dormant entity paths. |
| **V5: Entity & Conflict Resolution** | `ce21a54` | 16:49:04 | 17:04:24 | **15.3 min** | Multi-signal entity disambiguation engine (name tokens, org numbers, address proximity) and multi-view conflict resolution for discrepant sources. | Strict zero wrong-company gate tested on ambiguous names. |
| **V6: Temporal Evidence & Change Detection** | `6ea49d5` | 17:04:24 | 17:21:00 | **16.6 min** | Snapshot difference engine, deterministic historical fact timelines, effective/retrieved date tracking, and change explanations. | Temporal change tests pass; accurately records multi-year shifts. |
| **V7: Source Quality & Calibration** | `adfa0c5` | 17:21:00 | 17:40:08 | **19.1 min** | Evidence grounding layer enforcing verbatim claim spans, SHA256 content fingerprints, source authority scoring, and confidence calibration. | Grounded claims contract compliance verified. |
| **V8: Research Budget Optimization** | `f64a207` | 17:40:08 | 17:48:50 | **8.7 min** | Deterministic expected-yield scoring `(P(success) × InfoValue × Authority) / Cost` and early stopping on low-information profiles. | Requests reduced; expected-yield telemetry added. |
| **V9: Batch Scaling & Telemetry** | `55b9d63` | 17:48:50 | 18:08:48 | **20.0 min** | Concurrency engine (thread pool), HTTP status tracking (200, 429, 403, 503), zero-silent-drop validator, and terminal envelope emission. | 100-company batch run completes with valid envelopes. |
| **V10: Tier 1 Bulk BRREG Integration** | `e850d2b` | 18:08:48 | 18:53:35 | **44.8 min** | Pre-indexed local bulk statutory snapshot integration (`brreg-enheter.csv`, 1.17M rows) serving as Tier 1 source of truth, bypassing live statutory API lookups. | Baseline requests cut from 12+ per company down to ~2.0 req/co. |

---

## 3. Post-V10 Production Hardening & Budget Optimization

Following V10, three targeted hardening cycles were executed to satisfy strict competition constraints (≤ 2,000 requests per 1,000 companies and zero recall degradation):

### Cycle A: Housing Financials Routing Recovery
* **Time Taken**: ~35 minutes
* **Issue**: Borettslag/Sameie entities (`BRL`/`ESEK`) with statutory annual accounts were inadvertently skipped by general housing heuristics.
* **Resolution**: Added `has_accounts` inspection in planner routing (`planner.py`), restoring 30/30 housing financial claims in the benchmark.

### Cycle B: Operational Location Safety Audit
* **Time Taken**: ~45 minutes
* **Issue**: Audited all 13 location candidate companies from the benchmark to prevent false-positive subunit deferrals.
* **Resolution**: Established safe criteria: locations are only skipped when an entity strictly has **0 registered employees AND `harRegistrertAntallAnsatte: false`**. Preserved 11 operating companies' branch structures while safely skipping dormant holding entities.

### Cycle C: Website Crawl Budget Optimization
* **Time Taken**: ~50 minutes
* **Issue**: Website crawling on franchisors (`7-eleven.no`) and redirected domains consumed requests before entity verification rejected the observations.
* **Resolution**:
  1. Implemented pre-crawl homepage identity check (`assess_website_identity`): secondary pages are aborted immediately if the homepage fails identity gating.
  2. Reused parsed `RobotFileParser` across secondary pages to prevent duplicate `/robots.txt` calls.
  3. Capped secondary page depth to `max_secondary_pages = 1`.
* **Benchmark Result (`v10-crawl-optimized-100`)**:
  * Request count dropped from 200 &rarr; **180** (**1.80 requests/company**).
  * Recall and evidence: **100% preserved (751 claims, 992 historical facts, 915 temporal changes)**.

---

## 4. Benchmark Progression Summary

| Benchmark Run ID | Stage / Optimization | Requests (100 Co.) | Req / Co. | Runtime | Throughput | Grounded Claims | 1,000 Co. Projected Requests | Safety Headroom |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `v10-pre-fix` | Initial V10 Bulk Baseline | 202 | 2.02 | 70.1s | 85.6 co/min | 721 (housing missing) | 2,020 (Exceeds limit) | -20 (Fail) |
| `v10-final-regression-100` | Housing Financials Restored | 202 | 2.02 | 70.1s | 85.6 co/min | 751 (+30 restored) | 2,020 | -20 (Fail) |
| `v10-location-safe-100` | Location Deferral Hardened | 200 | 2.00 | 69.5s | 86.3 co/min | 751 | 2,000 (At limit) | 0 (Zero margin) |
| **`v10-crawl-optimized-100`** | **Homepage Identity + Crawl Cap** | **180** | **1.80** | **52.9s** | **113.5 co/min** | **751 (100% match)** | **1,800** | **+200 (Pass)** |
