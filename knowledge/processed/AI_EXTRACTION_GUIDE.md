# AI Article Extraction Guide

Use this guide when you analyze F1 articles in external AI tools and want valid output for APEX-F1.

This guide is optimized for:
- consistent signal quality
- minimal noise from speculative or tabloid content
- strict compatibility with `pipeline/validate_signals.py`

---

## 1) What The AI Should Do

For each F1 article:
1. Decide whether the article contains actionable race-performance information.
2. If yes, extract structured signals only (no narrative).
3. Output JSON in the exact schema below.

If an article is not relevant for performance modeling, do not produce a signal for it.

---

## 2) Relevance Filter (Very Important)

Create signals only for information likely to affect race outcome probabilities:

Relevant examples:
- Car upgrades (aero, floor, wing, PU-related performance/reliability context)
- Reliability risks (engine issues, cooling issues, recurring failures)
- Driver state that can affect pace/consistency (confidence, injury, major adaptation issues)
- Team strategy capability shifts (pitwall process changes, repeated strategic execution issues)
- Weather/track-condition insights specifically tied to team/driver readiness

Ignore or heavily down-weight:
- Lifestyle/gossip/personal stories
- PR-only content without concrete technical/competitive implication
- Pure opinion pieces with no evidence
- General F1 politics that do not materially affect next-race performance

If uncertain, skip.

---

## 3) Required JSON Output Format

Top-level accepted shape (recommended):

```json
{
  "signals": [
    {
      "team": "Ferrari",
      "upgrade_detected": true,
      "upgrade_component": "rear wing",
      "upgrade_magnitude": "medium",
      "reliability_concern": 0.2,
      "driver_confidence_change": 0.0,
      "source_confidence": 0.86,
      "source_name": "autosport",
      "article_hash": "sha256_of_normalized_title_plus_url",
      "extraction_version": "v1"
    }
  ]
}
```

Signal-level required fields:
- `source_name`: non-empty string
- `article_hash`: non-empty string
- `source_confidence`: number in `[0,1]`
- At least one of:
  - `team` (string), or
  - `driver` / `driver_name` (string)

Conditional:
- If `upgrade_detected` is `true`, then `upgrade_magnitude` must be exactly one of:
  - `minor`
  - `medium`
  - `major`

Range constraints:
- `reliability_concern`: `[0,1]`
- `driver_confidence_change`: `[-1,1]`

Do not include text outside JSON.

---

## 4) Field Semantics

`source_confidence`:
- Confidence in extracted claim quality (not outlet quality alone).
- 0.9+ only for concrete, corroborated reporting.
- 0.5-0.7 for weaker, speculative claims.

`reliability_concern`:
- 0.0 = no concern
- 0.5 = moderate risk signal
- 1.0 = severe, likely failure risk

`driver_confidence_change`:
- +1.0 strong positive confidence shift
- 0.0 neutral/no usable signal
- -1.0 strong negative confidence shift

`upgrade_magnitude`:
- `minor` = small/local gain expected
- `medium` = noticeable but not transformative
- `major` = substantial package change with meaningful pace impact potential

---

## 5) One Article -> Multiple Signals

If one article contains multiple independent claims, emit multiple signal objects in one `signals` array.

Example:
- Team upgrade claim + driver confidence claim => 2 signals.

Keep each signal atomic and specific.

---

## 6) Deduplication and Conflict Rules

- Do not duplicate the same claim from the same article as two signals.
- If article has conflicting statements, either:
  - use lower `source_confidence`, or
  - skip extraction if ambiguity is too high.

---

## 7) Article Hash (How To Fill)

`article_hash` should be:
- `sha256(normalized_title + normalized_url)`
- normalization: trim and collapse whitespace.

If your external AI cannot compute hash reliably:
- keep a deterministic placeholder during extraction,
- then replace before commit using your local helper workflow.

Never leave `article_hash` empty.

---

## 8) Copy-Paste Prompt For External AI

Use this directly in your external AI tool:

```text
You are an information extraction engine for Formula 1 race modeling.

Task:
Extract only performance-relevant structured signals from the provided F1 article.
Do not summarize. Do not explain. Return JSON only.

Rules:
1) Only include actionable claims likely to affect race probabilities (upgrades, reliability, driver confidence, strategy readiness).
2) Ignore gossip, PR fluff, and non-performance content.
3) Output must be:
{
  "signals": [ ... ]
}
4) Each signal must include:
   - source_name (string)
   - article_hash (string, non-empty)
   - source_confidence (0..1)
   - and at least one of team or driver/driver_name
5) If upgrade_detected=true, upgrade_magnitude must be one of minor|medium|major.
6) Keep reliability_concern in 0..1 and driver_confidence_change in -1..1.
7) Use extraction_version="v1".
8) If no valid performance signal exists, return:
{
  "signals": []
}

Return JSON only. No prose.
```

Note for APEX-F1 repo use:
- Do not commit files with empty `signals` array; skip creating signal file in that case.

---

## 9) Pre-Commit Checklist

Before committing `knowledge/processed/signals_YYYY-MM-DD.json`:
1. Run:
   - `python pipeline/validate_signals.py --signals-dir knowledge/processed --log-level INFO`
2. Ensure no schema/range errors.
3. Ensure each signal is performance-relevant and non-duplicative.
