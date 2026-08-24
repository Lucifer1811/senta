"""
src/retrieval/aggregator.py — evidence aggregator for Phase 3 inference.

Responsibilities:
  1. attach_outcomes()         — batch-join outcome_labels to analog dicts from get_analogs()
  2. label_distribution()      — compute label frequency dict from a list of labeled dicts
  3. assemble_evidence_pack()  — render the markdown evidence pack string + evidence_ids registry

The evidence pack is the single input to the LLM inference step. Aggregate distributions appear
before individual rows in both channels so the LLM anchors on the empirical prior first.

Evidence_id formats:
  - Analog:   "analog:{symbol}:{date}"   e.g. "analog:INFY:2023-10-11"
  - Text doc: "doc:{doc_id}"             e.g. "doc:48812"
"""

from __future__ import annotations

import logging
from datetime import date, timezone
from typing import Sequence

import psycopg

logger = logging.getLogger(__name__)

LABEL_ORDER = ["EXTREMELY_BEARISH", "BEARISH", "NEUTRAL", "BULLISH", "EXTREMELY_BULLISH"]
_LABEL_SET  = set(LABEL_ORDER)

# Unconditional population base rates from IS period (2024-01-01 to 2025-12-31, h=1).
# Frozen to IS period — must NOT be recomputed from the OOS/backtest window.

# label_version=1 — sector-residual, ±1%/±3% boundaries. N=24,896. 50-stock large cap panel.
BASE_RATES: dict[str, float] = {
    "EXTREMELY_BEARISH": 0.0116,
    "BEARISH":           0.1408,
    "NEUTRAL":           0.6899,
    "BULLISH":           0.1396,
    "EXTREMELY_BULLISH": 0.0181,
}

# label_version=4 — raw close-to-close, ±0.5%/±1.5% boundaries. 50-stock large cap panel.
# IS window 2024-01-01 → 2025-12-31, N=24,899
BASE_RATES_V4: dict[str, float] = {
    "EXTREMELY_BEARISH": 0.1210,
    "BEARISH":           0.2089,
    "NEUTRAL":           0.3256,
    "BULLISH":           0.2031,
    "EXTREMELY_BULLISH": 0.1415,
}

# label_version=5 — prev-close-to-next-high, ±0.5%/±1.5% boundaries. 50-stock large cap panel.
# IS window 2024-01-01 → 2025-12-31, N=24,899
BASE_RATES_V5: dict[str, float] = {
    "EXTREMELY_BEARISH": 0.0055,
    "BEARISH":           0.0157,
    "NEUTRAL":           0.2568,
    "BULLISH":           0.4343,
    "EXTREMELY_BULLISH": 0.2878,
}

# label_version=6 — residual prev-close-to-next-high, ±0.5%/±1.5% boundaries. 50-stock panel.
# IS window 2024-01-01 → 2025-12-31, N=24,899
BASE_RATES_V6: dict[str, float] = {
    "EXTREMELY_BEARISH": 0.0047,
    "BEARISH":           0.0304,
    "NEUTRAL":           0.2474,
    "BULLISH":           0.4191,
    "EXTREMELY_BULLISH": 0.2984,
}

# Lookup by label_version — callers can use get_base_rates(lv) instead of the constant.
_BASE_RATES_BY_VERSION: dict[int, dict[str, float]] = {
    1: BASE_RATES,
    4: BASE_RATES_V4,
    5: BASE_RATES_V5,
    6: BASE_RATES_V6,
}


def get_base_rates(label_version: int = 1) -> dict[str, float]:
    """Return the frozen IS base rates for the given label_version."""
    return _BASE_RATES_BY_VERSION.get(label_version, BASE_RATES)


# ── Step 1: analog outcome join ───────────────────────────────────────────────

def attach_outcomes(
    analogs: list[dict],
    conn: psycopg.Connection,
    label_version: int = 1,
    horizons: tuple[int, ...] = (1,),
) -> list[dict]:
    """
    Batch-join outcome_labels for a list of analog dicts (from get_analogs()).
    Adds 'outcomes': {horizon: label_str} to each dict in-place.
    Analogs without any resolved label get outcomes={}.
    Returns the same list (mutated).
    """
    if not analogs:
        return analogs

    syms  = [a["symbol"] for a in analogs]
    dates = [a["date"]   for a in analogs]

    with conn.cursor() as cur:
        cur.execute(
            """SELECT symbol, date, horizon_days, label
               FROM   outcome_labels
               WHERE  label_version = %s
                 AND  horizon_days = ANY(%s)
                 AND  (symbol, date) IN (
                     SELECT * FROM unnest(%s::text[], %s::date[])
                 )""",
            (label_version, list(horizons), syms, dates),
        )
        rows = cur.fetchall()

    outcomes_map: dict[tuple, dict[int, str]] = {}
    for sym, dt, h, label in rows:
        key = (sym, dt)
        if key not in outcomes_map:
            outcomes_map[key] = {}
        if label is not None:
            outcomes_map[key][h] = label

    for analog in analogs:
        analog["outcomes"] = outcomes_map.get((analog["symbol"], analog["date"]), {})

    return analogs


# ── Step 2a: label distribution helper ───────────────────────────────────────

def label_distribution(items: Sequence[dict], horizon: int = 1) -> dict[str, float]:
    """
    Compute label frequency fractions from a list of analog or text-precedent dicts.
    Keys are LABEL_ORDER members; only labels with count > 0 are included.
    Returns empty dict if no items have a resolved label at `horizon`.
    """
    counts: dict[str, int] = {}
    total = 0
    for item in items:
        label = item.get("outcomes", {}).get(horizon)
        if label and label in _LABEL_SET:
            counts[label] = counts.get(label, 0) + 1
            total += 1
    if total == 0:
        return {}
    return {lbl: counts[lbl] / total for lbl in LABEL_ORDER if lbl in counts}


def _fmt_dist(dist: dict[str, float]) -> str:
    """Format label distribution as "BULLISH 36% · NEUTRAL 29% · BEARISH 24%"."""
    if not dist:
        return "no labeled analogs"
    parts = [f"{lbl} {int(round(frac * 100))}%" for lbl, frac in dist.items()]
    return " · ".join(parts)


def _fmt_signal(
    dist: dict[str, float],
    base_rates: dict[str, float] | None = None,
    threshold_pp: int = 10,
) -> str:
    """
    Compare dist against base_rates and summarise which labels are elevated.
    Returns a human-readable signal string for the evidence pack.
    threshold_pp: minimum percentage-point lift to count as a signal.
    """
    if base_rates is None:
        base_rates = BASE_RATES
    if not dist:
        return "flat (no labeled analogs)"
    elevated = []
    for lbl in LABEL_ORDER:
        prior_pct = dist.get(lbl, 0.0) * 100
        base_pct  = base_rates.get(lbl, 0.0) * 100
        lift      = prior_pct - base_pct
        if lift >= threshold_pp:
            elevated.append(f"{lbl} +{lift:.0f}pp")
    if not elevated:
        return "flat — prior tracks base rates, no directional analog signal"
    return "ELEVATED: " + ", ".join(elevated)


def _fmt_count_dist(items: Sequence[dict], horizon: int = 1) -> str:
    """Format label distribution as counts: "BULLISH 3 · NEUTRAL 4 · BEARISH 1"."""
    counts: dict[str, int] = {}
    for item in items:
        label = item.get("outcomes", {}).get(horizon)
        if label and label in _LABEL_SET:
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "no labeled precedents"
    parts = [f"{lbl} {counts[lbl]}" for lbl in LABEL_ORDER if lbl in counts]
    return " · ".join(parts)


# ── Step 2b: evidence pack assembler ─────────────────────────────────────────

_TOP_N_ANALOGS = 15   # individual rows shown in the pack
_TOP_N_TEXT    = 10


def assemble_evidence_pack(
    symbol:              str,
    company_name:        str,
    sector:              str,
    target_date:         date,
    text_headline_count: int,
    same_analogs:        list[dict],
    cross_analogs:       list[dict],
    text_precedents:     list[dict],
    horizon:             int = 1,
    base_rates:          dict[str, float] | None = None,
) -> tuple[str, list[str]]:
    """
    Render the markdown evidence pack and return (pack_str, evidence_ids).

    evidence_ids is a list of all valid ids in this pack — the LLM parser validates
    that key_drivers only cite ids from this list.

    Aggregate distribution appears BEFORE individual rows in both channels.
    base_rates: frozen IS prior for the active label_version (defaults to v1 rates).
    """
    if base_rates is None:
        base_rates = BASE_RATES

    all_analogs = same_analogs + cross_analogs
    evidence_ids: list[str] = []
    lines: list[str] = []

    # Header
    lines += [
        "## SENTA Evidence Pack",
        f"**Symbol:** {symbol} — {company_name} — {sector}",
        f"**Date:** {target_date}  **Inference horizon:** {horizon} trading day",
        "",
        "---",
        "",
    ]

    # ── Analog Channel ────────────────────────────────────────────────────────
    n_all    = len(all_analogs)
    n_same   = len(same_analogs)
    n_cross  = len(cross_analogs)
    dist_all  = label_distribution(all_analogs, horizon)
    dist_same = label_distribution(same_analogs, horizon)

    signal_str = _fmt_signal(dist_all, base_rates=base_rates)
    is_flat    = signal_str.startswith("flat")

    lines += [
        f"### Analog Channel  (PRESENT — {n_all} analogs)",
        "Historical trading days with the most similar market microstructure to today.",
        "Outcomes are realized 1-day raw close-to-close returns "
        "(BEARISH < −0.5%, BULLISH > +0.5%, extremes outside ±1.5%).",
        "",
        f"**Aggregate (all {n_all}):** {_fmt_dist(dist_all)}",
        f"**Base rates (population):** {_fmt_dist(base_rates)}",
        f"**Analog signal:** {signal_str}",
    ]
    if n_same > 0:
        lines.append(f"**Same-symbol ({symbol} only):** {_fmt_dist(dist_same)} (n={n_same})")

    # Analog date range — helps the LLM weigh era-specific analogs
    if all_analogs:
        analog_dates = sorted(str(a["date"]) for a in all_analogs if "date" in a)
        if analog_dates:
            lines.append(f"**Analog period:** {analog_dates[0]} to {analog_dates[-1]}")

    lines.append("")

    # Individual rows (top-N by similarity, all_analogs already ordered by rank)
    if all_analogs:
        lines.append(f"Top {min(_TOP_N_ANALOGS, n_all)} by similarity:")
        lines.append("")
        if is_flat:
            lines += [
                "⚠ FLAT SIGNAL — the aggregate distribution above is near-uniform (no class "
                "elevated ≥10pp above base rates). Individual row outcomes below are historical "
                "noise. Do NOT count them for direction — see Rule 8.",
                "",
            ]
        lines.append("| evidence_id | Symbol | Date | Similarity | Outcome H1 |")
        lines.append("|-------------|--------|------|------------|------------|")
        for a in all_analogs[:_TOP_N_ANALOGS]:
            eid     = f"analog:{a['symbol']}:{a['date']}"
            outcome = a.get("outcomes", {}).get(horizon, "—")
            sim     = f"{a.get('similarity', 0):.3f}"
            lines.append(f"| {eid} | {a['symbol']} | {a['date']} | {sim} | {outcome} |")
            evidence_ids.append(eid)
    lines += ["", "---", ""]

    # ── Text Channel ──────────────────────────────────────────────────────────
    if text_headline_count == 0:
        lines += [
            "### Text Channel  (ABSENT — 0 headlines in corpus)",
            "This symbol has no news coverage. Inference based on analog channel only.",
            "",
        ]
    else:
        n_prec = len(text_precedents)
        lines += [
            f"### Text Channel  (PRESENT — {text_headline_count} headlines · {n_prec} precedents found)",
            "News and corporate events visible before market open (before 09:15 IST).",
            "Outcome is the 1-day return realized on the event_day (last market close before publication).",
            "",
            f"**Aggregate ({n_prec} precedents):** {_fmt_count_dist(text_precedents, horizon)}",
            "",
        ]
        if text_precedents:
            lines.append("| evidence_id | Event day | Type | Outcome H1 | Content |")
            lines.append("|-------------|-----------|------|------------|---------|")
            for doc in text_precedents[:_TOP_N_TEXT]:
                eid     = f"doc:{doc['doc_id']}"
                outcome = doc.get("outcomes", {}).get(horizon, "—")
                eday    = str(doc.get("event_day", "—"))
                dtype   = doc.get("doc_type", "")
                content = (doc.get("content") or "").replace("|", "\\|").replace("\n", " ")[:100]
                lines.append(f"| {eid} | {eday} | {dtype} | {outcome} | {content} |")
                evidence_ids.append(eid)
        lines += ["", "---", ""]

    # Notes
    lines += [
        "### Notes",
        "- Macro context: NOT AVAILABLE in this version. Do not infer absence of macro news as a signal.",
        f"- Evidence pack cutoff: {target_date} 03:45 UTC (pre-market). No post-open information included.",
    ]

    # Explicit ID registry — model copies from this list rather than reconstructing from tables
    lines += ["", "### Valid evidence_ids", "Only cite IDs from this exact list in key_drivers:"]
    for eid in evidence_ids:
        lines.append(f"- `{eid}`")

    return "\n".join(lines), evidence_ids
