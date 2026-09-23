"""DOL LCA disclosure import.

`jobpilot import-lca FILE...` reads the quarterly/annual LCA disclosure files
(downloaded manually from dol.gov), counts certified filings per employer, and
fuzzy-matches employer names to our companies. The result is a positive
ranking signal and a UI badge -- never a hard filter.

Only CSV is read: the DOL publishes .xlsx, and reading that needs a new
dependency (openpyxl). Export the sheet to CSV first.
"""

from __future__ import annotations

import csv
import difflib
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

from sqlmodel import Session, select

from .. import db
from ..models import Company

_NAME_COLUMNS = ("EMPLOYER_NAME", "LCA_CASE_EMPLOYER_NAME", "EMPLOYER_BUSINESS_NAME")
_STATUS_COLUMNS = ("CASE_STATUS", "STATUS")

# Legal-form suffixes dropped before comparing names.
_LEGAL = {
    "inc", "incorporated", "llc", "l l c", "corp", "corporation", "co", "company", "ltd",
    "limited", "lp", "llp", "plc", "pbc", "pc", "na", "the", "dba",
}
# Descriptive words many employers add to their legal name ("Palantir Technologies").
_GENERIC = {
    "technologies", "technology", "tech", "labs", "lab", "systems", "software", "group",
    "holdings", "usa", "us", "america", "services", "solutions", "global", "international",
    "hq", "platforms", "platform",
}
FUZZY_CUTOFF = 0.92


def normalize_employer(name: str) -> str:
    text = name.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [w for w in text.split() if w not in _LEGAL]
    return " ".join(words)


def _core(name: str) -> str:
    words = [w for w in normalize_employer(name).split() if w not in _GENERIC]
    return " ".join(words)


def _pick(header: list[str], options: Iterable[str]) -> str | None:
    upper = {h.strip().upper(): h for h in header}
    for opt in options:
        if opt in upper:
            return upper[opt]
    return None


def read_lca_counts(path: Path) -> Counter[str]:
    """Certified filings per normalized employer name in one disclosure CSV."""
    if path.suffix.lower() in {".xlsx", ".xls"}:
        raise ValueError(
            f"{path.name} is a spreadsheet. Open it and export to CSV, then import the CSV "
            "(reading .xlsx directly would need the openpyxl dependency)."
        )
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    counts: Counter[str] = Counter()
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        name_col = _pick(header, _NAME_COLUMNS)
        status_col = _pick(header, _STATUS_COLUMNS)
        if name_col is None:
            raise ValueError(f"{path.name}: no employer-name column (expected one of {_NAME_COLUMNS})")
        for row in reader:
            if status_col and (row.get(status_col) or "").strip().lower() != "certified":
                continue
            name = normalize_employer(row.get(name_col) or "")
            if name:
                counts[name] += 1
    return counts


def match_companies(
    companies: list[Company], counts: Counter[str]
) -> dict[int, tuple[int, list[str]]]:
    """For each company id, (total filings, matched employer names)."""
    by_core: dict[str, list[str]] = {}
    for employer in counts:
        by_core.setdefault(_core(employer) or employer, []).append(employer)
    cores = list(by_core)

    out: dict[int, tuple[int, list[str]]] = {}
    for company in companies:
        target = _core(company.name) or normalize_employer(company.name)
        if not target:
            continue
        matched = list(by_core.get(target, []))
        if not matched:
            for close in difflib.get_close_matches(target, cores, n=3, cutoff=FUZZY_CUTOFF):
                matched.extend(by_core[close])
        total = sum(counts[m] for m in matched)
        out[company.id] = (total, sorted(matched))
    return out


def import_lca(sess: Session, paths: list[Path]) -> list[tuple[Company, int, list[str]]]:
    """Recompute LCA history for every company from the given files.

    Counts are replaced, not added, so re-importing the same file is safe.
    Pass every fiscal year you want counted in a single call.
    """
    counts: Counter[str] = Counter()
    for path in paths:
        counts.update(read_lca_counts(path))

    companies = list(sess.exec(select(Company)).all())
    matches = match_companies(companies, counts)
    report = []
    for company in companies:
        total, names = matches.get(company.id, (0, []))
        company.lca_filings_count = total
        company.has_lca_history = total > 0
        sess.add(company)
        report.append((company, total, names))
    db.log_event(
        sess,
        None,
        "lca_imported",
        files=[str(p) for p in paths],
        employers=len(counts),
        matched={c.name: t for c, t, _ in report if t},
    )
    sess.commit()
    return report
