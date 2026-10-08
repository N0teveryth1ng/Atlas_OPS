# PRD.md

## Purpose
Fresher (3 months internship); precision over recall; shortlist surfaced in the digest/dashboard.

## Pipeline
profile, query planner, collectors, normalize/dedupe, JD parser (regex+LLM), hard filters, skill matching, evaluator, verifier, ranker, decision engine (APPLY/REVIEW/SKIP), digest (no email; local dashboard), feedback, selfcheck, eval, label-check.

## Rules
experience tolerance 1 year; hard filters code; status machine + sent subset of passed; evidence-quote validation; separate verifier model.

## Interface
CLI + scheduled + local dashboard (email delivery cancelled).

## Sources
Remotive, RemoteOK, Adzuna (if keys), Greenhouse/Lever/Ashby (code exists; may be unreachable/unkeyed).

## Non-goals
auto-applying (Phase 8/PR B on hold).

## Metrics
targets: precision@10 >= 80%, 0 seniority pass-throughs. UNVERIFIED: current measured values (no verified human-labelled set).
