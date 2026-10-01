"""Shared PUMA project resolver for Cloud processors.

This module is intentionally conservative:
- Existing tracker tabs are the authoritative destinations.
- "Residence"/"Project" are optional for matching.
- QBO/customer prefixes before ":" and operational context such as
  "(Dock Pickup)" are ignored for comparison.
- Explicit known aliases are supported.
- Fuzzy spelling is suggestion-only; it never authorizes a write.
- Optional exact part-number evidence can confirm one tracker.
- Operational processors must not create a tracker when resolution fails.
"""

from __future__ import annotations

import re
import difflib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

TRACKER_SUFFIX = " - Project Tracker"

EXPLICIT_ALIASES = {
    "maddux": "Maddux Farmhouse",
    "maddux residence": "Maddux Farmhouse",
    "gough": "Gough Hastings Residence",
    "gough residence": "Gough Hastings Residence",
    "stoney shore": "Stony Shore",
    "stock": "Misc Stock Purchases",
}

CONTEXT_SUFFIX_PATTERNS = [
    re.compile(r"\s*\(\s*dock\s+pickup\s*\)\s*$", re.I),
    re.compile(r"\s*\(\s*housing\s+sample\s*\)\s*$", re.I),
    re.compile(r"\s*\(\s*sample\s*\)\s*$", re.I),
    re.compile(r"\s*-\s*owner\s*$", re.I),
    re.compile(r"\s*-\s*night\s+design\s*$", re.I),
    re.compile(r"\s*-\s*yoakum\s*$", re.I),
]

FUZZY_SUGGESTION_MIN = 0.78
FUZZY_MARGIN_MIN = 0.12


@dataclass
class Resolution:
    status: str
    input_name: str
    normalized_key: str
    tracker_title: str = ""
    canonical_project: str = ""
    method: str = ""
    reasons: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    suggestions: List[Tuple[str, float]] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED" and bool(self.tracker_title)


def strip_context(value: str) -> str:
    s = str(value or "").strip()
    for rx in CONTEXT_SUFFIX_PATTERNS:
        s = rx.sub("", s).strip()
    return s


def normalize_project_key(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""

    if ":" in s:
        s = s.split(":")[-1].strip()

    s = strip_context(s)
    s = s.lower()
    s = re.sub(r"\b(residence|project)\b", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_subject_project(subject: str, process_tokens: Sequence[str]) -> str:
    """Extract a project-ish name without depending on one exact subject format."""
    s = str(subject or "").strip()
    s = re.sub(r"^(?:re|fwd?|fw)\s*:\s*", "", s, flags=re.I).strip()
    s = re.sub(r"[\u2010-\u2015]", "-", s)

    tokens = [re.escape(t) for t in process_tokens if t]
    if tokens:
        token_rx = "(?:" + "|".join(tokens) + ")"
        # Prefix form: "RFPS - Project"
        s = re.sub(rf"^\s*{token_rx}\b\s*[-:]?\s*", "", s, flags=re.I)
        # Suffix form: "Project - Delivery Report"
        s = re.sub(rf"\s*[-:]?\s*{token_rx}\b\s*$", "", s, flags=re.I)

    # Remove common operational notes but preserve the business name itself.
    s = re.sub(r"\s*\(\s*dock\s+pickup\s*\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*\(\s*housing\s+sample\s*\)\s*$", "", s, flags=re.I)
    return s.strip(" -:–—")


def list_tracker_titles(sheets_service, spreadsheet_id: str) -> List[str]:
    meta = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id
    ).execute().get("sheets", [])
    return [
        sh["properties"]["title"]
        for sh in meta
        if sh["properties"]["title"].endswith(TRACKER_SUFFIX)
        and sh["properties"]["title"] != TRACKER_SUFFIX
        and sh["properties"]["title"].strip() != TRACKER_SUFFIX.strip()
    ]


def tracker_base(title: str) -> str:
    return title[:-len(TRACKER_SUFFIX)].strip() if title.endswith(TRACKER_SUFFIX) else title.strip()


def _build_name_index(titles: Sequence[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for title in titles:
        base = tracker_base(title)
        key = normalize_project_key(base)
        if key:
            out.setdefault(key, []).append(title)

    # Known explicit aliases.
    base_lookup = {
        normalize_project_key(tracker_base(t)): t
        for t in titles
        if normalize_project_key(tracker_base(t))
    }
    for alias, target_name in EXPLICIT_ALIASES.items():
        target = base_lookup.get(normalize_project_key(target_name))
        if target:
            out.setdefault(normalize_project_key(alias), [])
            if target not in out[normalize_project_key(alias)]:
                out[normalize_project_key(alias)].append(target)
    return out


def _canon_part(value: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", str(value or "").upper().replace(" ", ""))


def _tracker_part_evidence(
    sheets_service,
    spreadsheet_id: str,
    tracker_titles: Sequence[str],
    parts: Sequence[str],
) -> Dict[str, int]:
    """Count DISTINCT exact requested part hits per tracker. Read-only.

    A single part hit is supporting evidence only because common parts can appear
    across unrelated jobs. Two or more distinct requested parts on one tracker
    can be strong corroboration when there is one unique winner.
    """
    wanted = {_canon_part(p) for p in parts if _canon_part(p)}
    if not wanted:
        return {}

    scores: Dict[str, int] = {}
    for title in tracker_titles:
        try:
            res = sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{title}'!A1:U"
            ).execute()
        except Exception:
            continue

        rows = res.get("values", []) or []
        if not rows:
            continue

        header_row = None
        part_col = None
        for ridx, row in enumerate(rows[:10]):
            normalized = [str(x or "").strip().lower() for x in row]
            for cidx, h in enumerate(normalized):
                if h in {"part number", "part #", "part no", "item #", "sku", "item", "item no"}:
                    header_row = ridx
                    part_col = cidx
                    break
            if part_col is not None:
                break

        if part_col is None:
            continue

        matched_parts = set()
        for row in rows[(header_row or 0) + 1:]:
            if part_col < len(row):
                part = _canon_part(row[part_col])
                if part in wanted:
                    matched_parts.add(part)

        if matched_parts:
            scores[title] = len(matched_parts)

    return scores

def resolve_existing_tracker(
    sheets_service,
    spreadsheet_id: str,
    input_name: str,
    *,
    parts: Optional[Sequence[str]] = None,
) -> Resolution:
    """Resolve an operational event to ONE existing tracker or refuse to guess."""
    original = str(input_name or "").strip()
    key = normalize_project_key(original)
    titles = list_tracker_titles(sheets_service, spreadsheet_id)
    index = _build_name_index(titles)

    result = Resolution(
        status="UNKNOWN",
        input_name=original,
        normalized_key=key,
    )

    # Conservative normalized/exact alias.
    matches = index.get(key, []) if key else []
    if len(matches) == 1:
        title = matches[0]
        result.status = "CONFIRMED"
        result.tracker_title = title
        result.canonical_project = tracker_base(title)
        result.method = "NORMALIZED_ALIAS"
        result.reasons.append("Unique project-registry match after conservative normalization.")
        return result
    if len(matches) > 1:
        result.status = "CONFLICT"
        result.method = "AMBIGUOUS_ALIAS"
        result.conflicts.append("Normalized project name maps to multiple existing trackers.")
        result.suggestions = [(tracker_base(t), 1.0) for t in matches]
        return result

    # If the name exactly belongs to Open Projects but no tracker exists, do
    # not let generic part evidence reroute the event to another job.
    open_matches = [
        name for name in _open_project_names(sheets_service, spreadsheet_id)
        if normalize_project_key(name) == key
    ] if key else []

    if len(open_matches) == 1:
        result.status = "REVIEW"
        result.method = "OPEN_PROJECT_TRACKER_MISSING"
        result.canonical_project = open_matches[0]
        result.reasons.append(
            "Project exists in Open Projects but no existing tracker resolves it."
        )
        result.suggestions = [(open_matches[0], 1.0)]
        return result

    if len(open_matches) > 1:
        result.status = "CONFLICT"
        result.method = "OPEN_PROJECT_COLLISION"
        result.conflicts.append(
            "Multiple Open Projects rows normalize to the same project identity."
        )
        result.suggestions = [(name, 1.0) for name in open_matches]
        return result

    # Exact part-number evidence: one distinct part is suggestion-only.
    # Two or more distinct requested parts on one unique tracker may confirm.
    if parts:
        part_hits = _tracker_part_evidence(sheets_service, spreadsheet_id, titles, parts)
        if part_hits:
            max_hits = max(part_hits.values())
            winners = [t for t, n in part_hits.items() if n == max_hits]
            if len(winners) == 1 and max_hits >= 2:
                title = winners[0]
                result.status = "CONFIRMED"
                result.tracker_title = title
                result.canonical_project = tracker_base(title)
                result.method = "MULTI_PART_CROSSCHECK"
                result.reasons.append(
                    f"{max_hits} distinct exact requested parts uniquely point to this tracker."
                )
                return result

            result.status = "REVIEW"
            result.method = "PART_SUGGESTION_ONLY"
            result.reasons.append(
                "Part evidence is not strong enough by itself to authorize a write."
            )
            result.suggestions = [
                (tracker_base(t), float(part_hits[t]))
                for t in sorted(part_hits, key=lambda x: part_hits[x], reverse=True)[:5]
            ]
            return result

    # Fuzzy is suggestion-only.
    scored: List[Tuple[float, str]] = []
    if key:
        for title in titles:
            score = difflib.SequenceMatcher(
                None, key, normalize_project_key(tracker_base(title))
            ).ratio()
            if score >= FUZZY_SUGGESTION_MIN:
                scored.append((score, title))
        scored.sort(reverse=True)

    if scored:
        result.status = "REVIEW"
        result.method = "FUZZY_SUGGESTION"
        result.suggestions = [
            (tracker_base(title), round(score, 3))
            for score, title in scored[:5]
        ]
        result.reasons.append(
            "Similar project name found, but fuzzy spelling is suggestion-only and cannot authorize a write."
        )

    return result


def resolve_subject_to_existing_tracker(
    sheets_service,
    spreadsheet_id: str,
    subject: str,
    process_tokens: Sequence[str],
    *,
    parts: Optional[Sequence[str]] = None,
) -> Resolution:
    guess = clean_subject_project(subject, process_tokens)
    return resolve_existing_tracker(
        sheets_service,
        spreadsheet_id,
        guess,
        parts=parts,
    )


def _open_project_names(sheets_service, spreadsheet_id: str) -> List[str]:
    """Read Open Projects column A as the new-project authority."""
    try:
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="'Open Projects'!A2:A"
        ).execute()
    except Exception:
        return []
    out = []
    for row in res.get("values", []) or []:
        if row and str(row[0] or "").strip():
            out.append(str(row[0]).strip())
    return out


def resolve_okd_project(
    sheets_service,
    spreadsheet_id: str,
    input_name: str,
) -> Resolution:
    """Resolve OKD to an existing tracker OR one unique Open Projects entry.

    Unlike operational processors, OKD may create tracker/task tabs, but only when
    the intended project is independently present in the Open Projects registry.
    """
    existing = resolve_existing_tracker(
        sheets_service,
        spreadsheet_id,
        input_name,
    )
    if existing.confirmed:
        existing.method = "EXISTING_TRACKER_" + existing.method
        return existing

    original = str(input_name or "").strip()
    key = normalize_project_key(original)
    result = Resolution(
        status="UNKNOWN",
        input_name=original,
        normalized_key=key,
    )

    if not key:
        result.status = "CONFLICT"
        result.method = "BLANK_PROJECT"
        result.conflicts.append("OKD project name is blank after normalization.")
        return result

    names = _open_project_names(sheets_service, spreadsheet_id)
    exact = [name for name in names if normalize_project_key(name) == key]

    if len(exact) == 1:
        project = strip_context(exact[0]).strip()
        result.status = "CONFIRMED"
        result.canonical_project = project
        result.tracker_title = project + TRACKER_SUFFIX
        result.method = "OPEN_PROJECTS_REGISTRY"
        result.reasons.append(
            "No tracker exists yet, but the project uniquely matches Open Projects."
        )
        return result

    if len(exact) > 1:
        result.status = "CONFLICT"
        result.method = "OPEN_PROJECTS_COLLISION"
        result.conflicts.append(
            "More than one Open Projects entry normalizes to the same project identity."
        )
        result.suggestions = [(name, 1.0) for name in exact]
        return result

    # Fuzzy suggestions are still review-only for project creation.
    scored = []
    for name in names:
        score = difflib.SequenceMatcher(
            None, key, normalize_project_key(name)
        ).ratio()
        if score >= FUZZY_SUGGESTION_MIN:
            scored.append((score, name))
    scored.sort(reverse=True)

    if scored:
        result.status = "REVIEW"
        result.method = "OPEN_PROJECTS_FUZZY_SUGGESTION"
        result.suggestions = [(name, round(score, 3)) for score, name in scored[:5]]
        result.reasons.append(
            "A similar Open Projects entry exists, but spelling similarity cannot create a project."
        )

    return result
