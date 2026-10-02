# Builderr Signalpost Challenge: Specification & Reference Guide

This document summarizes the complete requirements, constraints, evaluation rubric, architecture workflow, and submission protocol for the **Builderr Signalpost Norwegian Company Research Agent Challenge**.

---

## 1. Challenge Overview & Objective

Build an autonomous agent that takes a Norwegian organisation number (`organisasjonsnummer`, 9 digits) and returns verified, auditable company facts from permitted public sources.

### Core Goals:
1. **Find More Facts (Recall & Coverage)**:
   - Search official public registries (Brønnøysundregistrene - Enhetsregisteret, Regnskapsregisteret).
   - Search company websites, jobs, leadership/roles, financial history, and public activity.
2. **Entity Grounding & Evidence (Zero Wrong-Company Tolerance)**:
   - Every fact must be attributed with a valid `source_url`, retrieval date (`retrieved_at`), `content_sha256`, and exact `claim_span`.
   - **Crucial**: A material wrong-company match or fabricated financial value immediately disqualifies the run!
3. **Honest Availability States**:
   - If information is missing, blocked, or not applicable, return explicit states (`not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`).
   - **Never turn absence into zero.** A missing company in the output batch fails the run.
4. **Change Detection & Current Profiles**:
   - Run against previous snapshots to detect and explain material changes idempotently.
5. **Synthesis & UX**:
   - Synthesize a useful profile explaining what the company does, key leadership, financials, what changed, and what remains unknown.

---

## 2. System Architecture & Workflow

The agent processes each company number through the following modular pipeline:

```
                  INPUT
           Norwegian Org. No.
                   │
                   ▼
         ┌──────────────────┐
         │ Company Resolver │  (Brønnøysund bulk & live API anchor)
         └────────┬─────────┘
                  │
                  ▼
         ┌──────────────────┐
         │ Research Planner │  (Identifies needed modules & URLs)
         └────────┬─────────┘
                  │
        ┌─────────┼──────────┐
        ▼         ▼          ▼
     Business   Finance    People
        │         │          │
        ▼         ▼          ▼
     Products  Ownership   Management
        │         │          │
        └─────────┼──────────┘
                  ▼
         ┌──────────────────┐
         │ Evidence Checker │  (Entity gate, URL, SHA256, verbatim span)
         └────────┬─────────┘
                  │
                  ▼
         ┌──────────────────┐
         │ Change Detector  │  (Snapshot diffing, material updates)
         └────────┬─────────┘
                  │
                  ▼
         ┌──────────────────┐
         │ Profile/Synthesis│  (Summary, unknowns, audit-grade explanation)
         └────────┬─────────┘
                  │
                  ▼
             FINAL OUTPUT      (JSONL terminal envelope)
```

---

## 3. Official Scoring Rubric (100 Points Total)

Scoring Version 2 (effective Round 1 from August 26, 2026):

| Criterion | Points | Details & Formula |
|---|---|---|
| **Recall and Coverage** | **50 pts** | Evaluated against Builderr's pooled, verified reference collection (built from all submissions + Builderr crawlers).<br>**Formula per information type:**<br>`Score = (70% × Company Coverage Rate) + (30% × Individual Fact Recall Rate)`.<br>*(Example: finding 30 of 50 job postings across 15 of 20 companies gives `(70% × 75%) + (30% × 60%) = 70.5%`)*. |
| **Precision and Evidence** | **30 pts** | Checks that each fact belongs to the right company and has valid evidence (`source_url`, `retrieved_at`, verbatim quote span). Unsupported facts lose points. A material wrong-company match blocks qualification. |
| **Synthesis & Usefulness** | **12 pts** | High-utility profile explaining what the company does, what changed, and what remains unknown, with sources for all conclusions. |
| **UX & Verifiability** | **8 pts** | Checks whether a user can easily find, compare, and verify company information on desktop and mobile. |

### Qualification Thresholds:
- **Minimum Score to Qualify**: **65 / 100** on an official run.
- **Current Competition Landscape**: As of October 2026, **28 submissions, 17 assessed, 0 QUALIFIED!** The highest score on the leaderboard is **45.59/100** (Karthik: Recall 17.86, Evidence 18.93, Synthesis 7.20, UX 1.60).
- **Hard Gate / Immediate Disqualification**:
  - Material wrong-company publication (e.g. confusing parent/subsidiary, similarly named brand).
  - Fabricated or hallucinated financial numbers.
  - Silent drops / missing companies (every supplied company must have a terminal envelope).

---

## 4. Common Traps & How Most Runs Fail (Builderr Guidance)

1. **Agent picks its own companies**: Builderr supplies the official batch (1,000–1,200 companies) at run time after the daily cutoff. The agent must read whatever file is provided.
2. **Handling only 1 company**: The agent must process the entire batch end-to-end within the time/resource budget.
3. **No single command to run it**: The submission MUST provide a single CLI command that Builderr can copy and paste.
4. **Fails on a clean machine**: Dependencies must be pinned (`uv.lock` or `pyproject.toml`) and install cleanly in an empty environment.
5. **Returns fewer results than supplied**: If 100 (or 1,000) companies are given, exactly 100 (or 1,000) envelopes must be emitted. If nothing is found, return `not_available` or `ambiguous`—never drop the row.
6. **Zero for missing values**: Missing employee count or accounts is `not_available`, not `0`.
7. **LLM Usage**: LLMs **are permitted**! A small external API budget is allowed. If model keys are needed for evaluation, Builderr can supply standard keys upon request (no private bespoke credentials).

---

## 5. Operational Constraints

- **Time Limit**: 45 minutes for standard batch runs.
- **Request Limit**: Up to 2,000 outbound HTTP requests per run.
- **Cost Limit**: ≤ $10 declared external API costs per run.
- **Universe**: 411,160 active Norwegian companies with 2025 annual account records.

---

## 6. What to Submit

Email **`submit@builderr.ai`** with:
1. **Repository Link & Exact Commit Hash**.
2. **100-company Smoke-Test Result or Run Report**.
3. **One Single Command to Run It**.
4. **Declared Models, APIs, Licences & Expected Cost per official run**.
5. **Agent Name & Contact Details**.

---

## 7. Required Output Contract

Each processed organisation number must emit exactly one JSON object envelope in the output JSONL file:

```json
{
  "organisation_number": "123456789",
  "run": {
    "run_id": "2026-08-24-a",
    "started_at": "2026-08-24T06:00:00Z",
    "completed_at": "2026-08-24T06:00:08Z",
    "terminal_status": "completed"
  },
  "claims": [
    {
      "field": "official_website",
      "value": "https://example.no/",
      "availability": "available",
      "confidence": 0.99,
      "evidence_ids": ["ev-1"]
    }
  ],
  "evidence": [
    {
      "id": "ev-1",
      "source_url": "https://example.no/",
      "source_class": "company_owned",
      "retrieved_at": "2026-08-24T06:00:04Z",
      "content_sha256": "...",
      "claim_span": "Example AS, organisation number 123 456 789"
    }
  ],
  "changes": [],
  "errors": [],
  "operations": {
    "requests": 4,
    "runtime_ms": 8120,
    "third_party_cost_usd": 0.0
  }
}
```

Allowed availability states: `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, and `failed`.

---

## 8. Version Roadmap (V0 to V10)

```
V0  Foundation / Baseline
 │
 ▼
V1  Research Synthesis
 │
 ▼
V2  External Jobs & Workforce
 │
 ▼
V3  Reviews & Local Footprint
 │
 ▼
V4  Adaptive Research Planner
 │
 ▼
V5  Entity Resolution + Conflict Resolution
 │
 ▼
V6  Temporal Updates / Change Detection
 │
 ▼
V7  Evidence & Source Quality
 │
 ▼
V8  Research Budget Optimization
 │
 ▼
V9  Batch Scaling → 1,000+ Profiles
 │
 ▼
V10 Final Evaluation + Submission
```

---

## 9. Benchmark Tracking Table

| Version | Recall | Evidence | Synthesis | UX | Total | Requests | Time |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **V0 Baseline** | — | 15/15 foundation | — | — | **23 proxy** | 56 (for 10) | 42s |
| **V1 Synthesis** | ? | ? | ? | ? | ? | ? | ? |
| **V2 + Jobs** | ? | ? | ? | ? | ? | ? | ? |
| **V3 + Reviews** | ? | ? | ? | ? | ? | ? | ? |
| **V4 Adaptive** | ? | ? | ? | ? | ? | ? | ? |
| **V5 Entity Resolution** | ? | ? | ? | ? | ? | ? | ? |
| **V6 Temporal Updates** | ? | ? | ? | ? | ? | ? | ? |
| **V7 Source Quality** | ? | ? | ? | ? | ? | ? | ? |
| **V8 Budget Optimization** | ? | ? | ? | ? | ? | ? | ? |
| **V9 Batch Scale (1000)** | ? | ? | ? | ? | ? | ? | ? |
| **V10 Submission** | ? | ? | ? | ? | ? | ? | ? |


