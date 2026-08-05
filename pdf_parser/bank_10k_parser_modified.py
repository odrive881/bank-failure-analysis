#!/usr/bin/env python3
"""
bank_10k_parser.py

Extracts key financial metrics from bank 10-K / 10-Q PDF filings and
exports them to JSON.

Target metrics (from the Consolidated Balance Sheet / Statement of
Financial Condition and the Consolidated Statement(s) of Income):

    - Total Assets
    - Net Income
    - Total Stockholders' / Shareholders' Equity
    - Total Noninterest Expense
    - Total Interest Income
    - Total Noninterest Income
    - Net Interest Income (before provision for credit losses)
    - Interest Income + Noninterest Income  (derived: sum of the two above)
    - Interest Expense on Deposits
    - Total Deposits (consolidated)
    - Accumulated Other Comprehensive Income / (Loss)

Designed against First Republic Bank and Signature Bank 10-K filings,
but built to be reasonably resilient to the label variations found in
10-Q filings (e.g. "Condensed Consolidated..." headers, "non-interest"
vs "noninterest", hyphenation differences, multiple reporting periods
per column, etc).

Usage
-----
    python3 bank_10k_parser.py FILE.pdf [FILE2.pdf ...] -o output.json
    python3 bank_10k_parser.py FILE.pdf --bank "First Republic Bank"

Requires poppler-utils (`pdftotext`) to be installed on the system.
Falls back to pdfplumber if pdftotext is unavailable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

def _looks_misaligned(text: str) -> bool:
    """Detect a layout-extraction failure mode seen with some ``pdftotext -layout``
    versions/filings: a row's numeric column values land on the wrong line relative to its
    label (e.g. "Total Deposits" is extracted with no number on its line, while an
    unrelated line above or below it picks up that row's numbers instead). This is uneven
    within a single document -- some rows stay correctly aligned while others don't -- so
    checking a single canary line (e.g. just "Total Assets") isn't reliable. Instead, probe
    every target line item's own pattern (``LINE_ITEMS``, ``find_section``,
    ``BALANCE_SHEET_HEADERS``, ``INCOME_STATEMENT_HEADERS`` are all defined further down but
    already fully defined by the time this runs) against the actual statement sections: if
    any line item matches a line with no trailing number, the extraction is untrustworthy
    and a different backend should be tried. A single hit is enough here (unlike a
    whole-document scan) precisely because this is scoped to the ~90-line statement window
    matched by ``find_section`` -- there's little room left for an unrelated, coincidental
    false positive within that narrow a slice.

    Deliberately scoped to just the balance-sheet/income-statement sections rather than the
    whole document -- a 10-K's narrative sections (MD&A, footnotes) routinely reference
    these same labels in running prose with no adjacent numbers, which would otherwise read
    as false-positive misalignment on long filings.
    """
    sections = [s for s in (
        find_section(text, BALANCE_SHEET_HEADERS),
        find_section(text, INCOME_STATEMENT_HEADERS),
    ) if s]
    if not sections:
        return False  # can't even locate the statements -- let the normal "not found" warning surface

    numberless_hits = 0
    for section in sections:
        for spec in LINE_ITEMS:
            for line in section.splitlines():
                stripped = line.strip()
                if stripped and spec.pattern.match(stripped):
                    if not extract_numbers_from_line(line):
                        numberless_hits += 1
                    break
    return numberless_hits >= 1


def _extract_text_pdfplumber(pdf_path: str) -> str:
    import pdfplumber
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return "\n\f".join(chunks)


def extract_text(pdf_path: str) -> str:
    """Extract text from a PDF, preserving layout (columns/spacing).

    Prefers ``pdftotext -layout``, but falls back to pdfplumber both when pdftotext is
    unavailable/fails outright, and when its output is present but fails the
    ``_looks_misaligned`` sanity check (some pdftotext versions misalign numeric columns
    for certain filings, silently producing plausible-looking but wrong text).
    """
    pdftotext_text = None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            check=True,
        )
        if result.stdout and result.stdout.strip():
            pdftotext_text = result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    if pdftotext_text is not None and not _looks_misaligned(pdftotext_text):
        return pdftotext_text

    pdfplumber_text = _extract_text_pdfplumber(pdf_path)
    if pdftotext_text is not None and _looks_misaligned(pdfplumber_text):
        # Both backends look suspect; keep pdftotext's take since there's no better signal.
        return pdftotext_text
    return pdfplumber_text


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

# Matches a parenthesized number, a plain number (w/ optional $ and commas),
# or an em-dash / double-hyphen used to represent zero / not applicable.
NUMBER_TOKEN_RE = re.compile(
    r"\(\$?\s?[\d,]+(?:\.\d+)?\)"      # (1,234) or ($1,234)
    r"|\$?\s?[\d,]+(?:\.\d+)?"        # 1,234 or $1,234
    r"|—|--"                          # em-dash / double-hyphen placeholder
)

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s+(19|20)\d{2}"
)


def parse_number(token: str) -> Optional[float]:
    """Convert a raw numeric token (as pulled from the filing text) to a
    Python float. Parenthesized values are treated as negative. Em-dashes
    / '--' are treated as zero (typical convention in these filings)."""
    tok = token.strip()
    if tok in ("—", "--", "-", ""):
        return 0.0
    negative = tok.startswith("(") and tok.endswith(")")
    tok = tok.strip("()").replace("$", "").replace(",", "").strip()
    if not tok:
        return None
    try:
        value = float(tok)
    except ValueError:
        return None
    return -value if negative else value


def extract_numbers_from_line(line: str) -> list[float]:
    """Pull every numeric value out of a line item row, in left-to-right
    (i.e. earliest-period-first, matching the filing's column order)
    order."""
    tokens = NUMBER_TOKEN_RE.findall(line)
    values = [parse_number(t) for t in tokens]
    return [v for v in values if v is not None]


def detect_units(text: str) -> str:
    """Look for '(in millions...)' / '(in thousands...)' style unit
    disclosures near the top of a statement. Returns 'millions',
    'thousands', or 'ones' (i.e. raw dollars, unscaled)."""
    # Some PDFs expose text with word spaces removed, e.g. ``(inmillions)``.
    m = re.search(r"in\s*(millions|thousands)", text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return "ones"


UNIT_MULTIPLIER = {"millions": 1_000_000, "thousands": 1_000, "ones": 1}


def detect_periods(header_block: str) -> list[str]:
    """Attempt to identify the reporting period labels (years or full
    dates) associated with each numeric column in a statement, based on
    the header lines that appear directly above the data rows."""
    dates = DATE_RE.findall(header_block)
    if dates:
        # DATE_RE captures groups; re-search for full matches instead.
        full_dates = re.findall(
            r"(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}",
            header_block,
        )
        if full_dates:
            return full_dates

    years = re.findall(r"\b(?:19|20)\d{2}\b", header_block)
    # de-duplicate while preserving order
    seen = []
    for y in years:
        if y not in seen:
            seen.append(y)
    return seen


# --------------------------------------------------------------------------
# Statement section location
# --------------------------------------------------------------------------

# Header patterns for the two statements we care about. Written to match
# both full-year ("Consolidated Balance Sheets") and quarterly
# ("Condensed Consolidated Statements of Financial Condition") titles.
BALANCE_SHEET_HEADERS = [
    r"CONSOLIDATED\s*BALANCE\s*SHEETS?",
    r"CONSOLIDATED\s*STATEMENTS?\s*OF\s*FINANCIAL\s*CONDITION",
]
INCOME_STATEMENT_HEADERS = [
    r"CONSOLIDATED\s*STATEMENTS?\s*OF\s*INCOME(?:\s*AND\s*COMPREHENSIVE\s*INCOME)?",
]

# Stop the section at the next major all-caps statement header, footnote
# reference block, or a page break followed by an unrelated section.
SECTION_STOP_RE = re.compile(
    r"^\s*(CONSOLIDATED\s*STATEMENTS?\s*OF|CONSOLIDATED\s*BALANCE|"
    r"See\s*accompanying\s*notes)",
    re.IGNORECASE,
)


DOT_LEADER_RE = re.compile(r"\.\s*\.\s*\.\s*\.")


def _looks_like_toc(window_text: str) -> bool:
    """Dot leaders (four-plus consecutive periods) are a strong signal of
    a Table of Contents entry -- real financial statement bodies never use
    them."""
    return bool(DOT_LEADER_RE.search(window_text))


def find_section(text: str, header_patterns: list[str], window: int = 90) -> Optional[str]:
    """Locate the real occurrence of a statement header (skipping Table of
    Contents entries, which match the same title text but are followed by
    a dot-leader and page number) and return a chunk of text below it,
    trimmed at the next statement header or notes reference."""
    for pattern in header_patterns:
        # Try exact ALL-CAPS matches first -- this is how these statements
        # are actually titled in the filings, and avoids false-positive
        # hits on lowercase/mixed-case narrative references to the same
        # statement elsewhere in the document (e.g. in Item 1 business
        # description text).
        for flags in (0, re.IGNORECASE):
            matches = list(re.finditer(pattern, text, flags))
            if matches:
                break
        for m in matches:
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            window_lines = text[line_start:].splitlines()[:3]
            if _looks_like_toc("\n".join(window_lines)):
                continue  # skip TOC entry, keep looking

            lines = text[line_start:].splitlines()
            collected = []
            for i, line in enumerate(lines):
                if i > 0 and SECTION_STOP_RE.match(line):
                    break
                collected.append(line)
                if i >= window:
                    break
            return "\n".join(collected)
    return None


# --------------------------------------------------------------------------
# Line item extraction
# --------------------------------------------------------------------------

@dataclass
class LineItemSpec:
    key: str
    label: str
    # Regex applied to the START of a (stripped) line to identify the row.
    pattern: re.Pattern
    take_first_match: bool = True  # stop scanning after first hit


LINE_ITEMS = [
    LineItemSpec(
        "total_assets", "Total Assets",
        re.compile(r"^Total\s*[Aa]ssets\b", re.IGNORECASE),
    ),
    LineItemSpec(
        "net_income", "Net Income",
        re.compile(r"^Net\s*[Ii]ncome\s*(\(loss\))?\s*(\.|\$|—|--|\d|$)", re.IGNORECASE),
    ),
    LineItemSpec(
        "stockholders_equity", "Total Stockholders'/Shareholders' Equity",
        re.compile(r"^Total\s*(Stockholders|Shareholders)[’'`]?\s*Equity\b", re.IGNORECASE),
    ),
    LineItemSpec(
        "noninterest_expense", "Total Noninterest Expense",
        re.compile(r"^Total\s*non-?interest\s*expense\b", re.IGNORECASE),
    ),
    LineItemSpec(
        "interest_income", "Total Interest Income",
        re.compile(r"^Total\s*interest\s*income\b", re.IGNORECASE),
    ),
    LineItemSpec(
        "noninterest_income", "Total Noninterest Income",
        re.compile(r"^Total\s*non-?interest\s*income\b", re.IGNORECASE),
    ),
    LineItemSpec(
        "net_interest_income", "Net Interest Income",
        # Banks report both a pre-provision subtotal and, a line or two
        # later, a post-provision one -- and the exact phrasing of the
        # pre-provision line varies by filer:
        #   - "Net interest income" (e.g. First Republic Bank)
        #   - "Net interest income before provision for credit losses"
        #     (e.g. Signature Bank)
        # Both must be matched, while "Net interest income after provision
        # for credit/loan losses" must not. Anchoring on a following
        # digit/$/dot-leader (as originally written) correctly rejected the
        # "after" row but also rejected "before provision..." verbiage,
        # since that isn't immediately followed by a number either -- that
        # was the actual bug. The fix targets only the word "after".
        re.compile(r"^Net\s*interest\s*income\b(?!\s*after)", re.IGNORECASE),
    ),
    LineItemSpec(
        "interest_expense_deposits", "Interest Expense on Deposits",
        # The "Deposits" sub-line under the "Interest expense:" header in the
        # income statement (distinct from "Total Deposits" on the balance
        # sheet, which uses a different, Total-prefixed pattern and lives in
        # a different section entirely). Balance sheets also use a bare
        # "Deposits" / "Deposits:" subheading (introducing an itemized
        # deposit-type breakdown below it, e.g. "Non-interest-bearing" /
        # "Certificates of deposit") with no trailing number of its own --
        # requiring a number-like character immediately after the word
        # excludes that subheading while still matching the real data row,
        # and avoids tripping _looks_misaligned()'s false-positive check.
        re.compile(r"^Deposits\b\s*(\.|\$|—|--|\d)", re.IGNORECASE),
    ),
    LineItemSpec(
        "total_deposits", "Total Deposits",
        re.compile(r"^Total\s*[Dd]eposits\b", re.IGNORECASE),
    ),
    LineItemSpec(
        "accumulated_oci", "Accumulated Other Comprehensive Income/(Loss)",
        re.compile(r"^Accumulated\s*other\s*comprehensive\s*(income|loss)", re.IGNORECASE),
    ),
]


# --------------------------------------------------------------------------
# Held-to-maturity securities (carrying value + fair value)
# --------------------------------------------------------------------------
#
# Unlike the LINE_ITEMS above (a label followed by trailing column numbers on
# one physical line), FRCB and SBNY both disclose HTM carrying value *and*
# fair value as a single balance-sheet row whose label wraps across 2-3
# physical lines, with the fair-value figures embedded in running prose
# rather than in a numeric column. Each bank phrases this differently, so
# each gets its own regex; both are anchored to a short window of joined
# lines (not the whole document) to keep false positives out, and use \s*
# instead of \s+ between words for the same reason the rest of this parser
# does -- pdfplumber's fallback text collapses inter-word spacing entirely
# for some filings (e.g. FRCB's), while pdftotext -layout keeps it (e.g.
# SBNY's).
#
# FRCB: "Debt securities held-to-maturity, net of allowance for credit
# losses of $9 and $7, respectively (fair value of $23,422 and $17,964,
# respectively) ....... 22,292 16,603" -- no explicit years; the two
# trailing numbers are the carrying values, paired positionally with the
# balance sheet's own detected column years (like every other LINE_ITEM).
FRCB_HTM_RE = re.compile(
    r"debt\s*securities\s*held-to-maturity,?\s*net\s*of\s*allowance\s*for\s*credit\s*losses\s*of\s*"
    r"\$?\s*([\d,]+)\s*and\s*\$?\s*([\d,]+),?\s*"
    r"respectively\s*\(\s*fair\s*value\s*of\s*\$?\s*([\d,]+)\s*and\s*\$?\s*([\d,]+),?\s*respectively\s*\)"
    r"[.\s]*\$?\s*([\d,]+)\s+\$?\s*([\d,]+)",
    re.IGNORECASE,
)

# SBNY: "Securities held-to-maturity (fair value $7,018,200 at December 31,
# 2022 and\n$4,944,777 at December 31, 2021); (allowance for credit losses
# $25 at\nDecember 31, 2022 and $56 at December 31, 2021) 7,780,374
# 4,998,281" -- the two trailing carrying-value numbers land at the very
# end, after an intervening allowance-for-credit-losses clause with its own
# dollar figures. The non-greedy ".*?" skips over that clause: it can't
# stop early since none of the allowance clause's own numbers are followed
# by another bare number (they're each followed by a word), so the first
# place two numbers appear back-to-back is the real carrying-value pair.
# Years are stated explicitly for the fair values, unlike FRCB's phrasing.
#
# The final two captures require an actual comma-grouped amount
# (\d{1,3}(?:,\d{3})+, e.g. "7,780,374") rather than the looser [\d,]+ used
# elsewhere in this parser: with the loose pattern, the non-greedy ".*?"
# stops on the very first digits-then-comma token it meets, which is "31,"
# from the "...December 31, 2022..." date inside the intervening allowance
# clause -- not a real dollar amount. Genuine carrying values in these
# filings always carry thousands separators, so this excludes bare day/year
# numbers while still matching the real trailing pair.
SBNY_HTM_RE = re.compile(
    r"securities\s*held-to-maturity\s*\(\s*fair\s*value\s*\$?\s*([\d,]+)\s*at\s*December\s*31,?\s*(\d{4})\s*and\s*"
    r"\$?\s*([\d,]+)\s*at\s*December\s*31,?\s*(\d{4})\s*\)"
    r".*?\$?\s*(\d{1,3}(?:,\d{3})+)\s+\$?\s*(\d{1,3}(?:,\d{3})+)",
    re.IGNORECASE | re.DOTALL,
)


def extract_htm_securities(bs_section: str, bs_periods: list[str]) -> dict:
    """Locate the held-to-maturity debt securities balance-sheet row and pull out
    both its carrying value and its (prose-embedded) fair value.

    Returns a dict with up to two keys, ``held_to_maturity_securities`` (carrying
    value) and ``held_to_maturity_securities_fair_value``, each shaped like a
    LINE_ITEMS result (``label_matched`` / ``raw_values`` keyed by period). Returns
    an empty dict if no recognized disclosure is found.
    """
    lines = bs_section.splitlines()
    anchor = re.compile(r"held-to-maturity", re.IGNORECASE)
    for i, line in enumerate(lines):
        if not anchor.search(line):
            continue
        window = "\n".join(lines[i:i + 3])

        match = FRCB_HTM_RE.search(window)
        if match:
            fair_cur, fair_prior, carry_cur, carry_prior = match.group(3, 4, 5, 6)
            label = "Debt securities held-to-maturity, net (FRCB-style disclosure)"
            return {
                "held_to_maturity_securities": {
                    "label_matched": label,
                    "raw_values": [parse_number(carry_cur), parse_number(carry_prior)],
                },
                "held_to_maturity_securities_fair_value": {
                    "label_matched": label,
                    "raw_values": [parse_number(fair_cur), parse_number(fair_prior)],
                },
            }

        match = SBNY_HTM_RE.search(window)
        if match:
            # Years are stated explicitly here, but always agree with the balance
            # sheet's own column order (current year first) -- kept positional
            # (raw_values, zipped against bs_periods by the caller) rather than
            # branching the output shape, since every other line item works that way.
            fair_cur, _year_cur, fair_prior, _year_prior, carry_cur, carry_prior = match.groups()
            label = "Securities held-to-maturity (SBNY-style disclosure)"
            return {
                "held_to_maturity_securities": {
                    "label_matched": label,
                    "raw_values": [parse_number(carry_cur), parse_number(carry_prior)],
                },
                "held_to_maturity_securities_fair_value": {
                    "label_matched": label,
                    "raw_values": [parse_number(fair_cur), parse_number(fair_prior)],
                },
            }
    return {}


def extract_line_items(section_text: str, specs: list[LineItemSpec]) -> dict:
    """Scan a statement section line-by-line and pull out numeric values
    for each requested line item spec."""
    results = {}
    lines = section_text.splitlines()
    found_keys = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        for spec in specs:
            if spec.key in found_keys and spec.take_first_match:
                continue
            if spec.pattern.match(stripped):
                values = extract_numbers_from_line(line)
                if values:
                    results[spec.key] = {
                        # Preserve the complete source row. The standardizer
                        # exposes this as ``source_label`` for auditability.
                        "label_matched": stripped,
                        "raw_values": values,
                    }
                    found_keys.add(spec.key)
    return results


# --------------------------------------------------------------------------
# Bank / filing type detection
# --------------------------------------------------------------------------

KNOWN_BANKS = {
    "first republic bank": "First Republic Bank",
    "signature bank": "Signature Bank",
}
# Fallback filename fragments, for cases where the cover page renders the
# registrant name as a logo/image rather than extractable text (seen in
# First Republic's filings).
FILENAME_HINTS = {
    "frcb": "First Republic Bank",
    "firstrepublic": "First Republic Bank",
    "signaturebank": "Signature Bank",
    "sbny": "Signature Bank",
}


def detect_bank(text: str, filename: str, override: Optional[str]) -> str:
    if override:
        return override
    haystack = text[:40000].lower()
    for needle, canonical in KNOWN_BANKS.items():
        if needle in haystack:
            return canonical
    fname_lower = filename.lower()
    for needle, canonical in FILENAME_HINTS.items():
        if needle in fname_lower:
            return canonical
    return "Unknown"


DASH_CLASS = r"[-\u2010\u2011\u2012\u2013\u2014]"


def detect_filing_type(text: str) -> str:
    head = text[:3000]
    if re.search(rf"FORM\s+10{DASH_CLASS}Q", head, re.IGNORECASE):
        return "10-Q"
    if re.search(rf"FORM\s+10{DASH_CLASS}K", head, re.IGNORECASE):
        return "10-K"
    return "Unknown"


# Calendar-quarter-end convention (period end month -> quarter). All banks
# in this project are calendar-year filers, consistent with quarter_end_date()
# in the downstream standardization script.
MONTH_TO_QUARTER = {
    "january": 1, "february": 1, "march": 1,
    "april": 2, "may": 2, "june": 2,
    "july": 3, "august": 3, "september": 3,
    "october": 4, "november": 4, "december": 4,
}


def period_label(filing_type: str, period_end_date: Optional[str]) -> str:
    """Build a short "10-K_2021" or "10-Q_2022_Q1" style label for a filing.

    Falls back to just the filing type if the period end date couldn't be
    parsed off the cover page (rather than raising or silently omitting the
    filing from the output name).
    """
    if not period_end_date:
        return filing_type
    year_match = re.search(r"(19|20)\d{2}", period_end_date)
    year = year_match.group(0) if year_match else None
    month_match = re.match(r"\s*([A-Za-z]+)", period_end_date)
    quarter = MONTH_TO_QUARTER.get(month_match.group(1).lower()) if month_match else None
    if not year:
        return filing_type
    if filing_type == "10-Q" and quarter:
        return f"{filing_type}_{year}_Q{quarter}"
    return f"{filing_type}_{year}"


def default_output_filename(results: list[dict], bank_override: Optional[str]) -> str:
    """Auto-generate an output filename encoding bank, filing type, and
    period(s) covered, e.g. ``parsed_financials_Signature_Bank_10-K_2021.json``
    or, for a batch of quarterly filings spanning a year, a compact range.
    """
    bank_name = bank_override or (results[0].get("bank") if results else None) or "output"
    bank_slug = re.sub(r"\s+", "_", bank_name.strip())

    labels = []
    for r in results:
        label = period_label(r.get("filing_type") or "Unknown", r.get("period_end_date"))
        if label not in labels:
            labels.append(label)

    if len(labels) == 1:
        period_slug = labels[0]
    elif len(labels) <= 4:
        period_slug = "-".join(labels)
    else:
        # Long batches (e.g. all 10-Qs for several years): collapse to a
        # first-to-last range rather than an unwieldy filename.
        period_slug = f"{labels[0]}_to_{labels[-1]}"

    return f"parsed_financials_{bank_slug}_{period_slug}.json"


def detect_period_end_date(text: str) -> Optional[str]:
    """Grabs the 'for the fiscal year ended...' / 'for the quarterly
    period ended...' date from the cover page."""
    m = re.search(
        r"(?:fiscal\s*year\s*ended|quarterly\s*period\s*ended)\s*"
        r"((?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s*\d{1,2},\s*\d{4})",
        text[:3000], re.IGNORECASE,
    )
    if not m:
        return None
    # Restore readable spacing when the PDF text layer concatenates words.
    date_text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", m.group(1))
    return re.sub(r",\s*", ", ", date_text)


# --------------------------------------------------------------------------
# Main per-file parse routine
# --------------------------------------------------------------------------

def build_period_value_map(raw_values: list[float], periods: list[str], multiplier: int) -> dict:
    """Zip extracted numeric values with detected period labels. If the
    counts don't line up, values are still returned (keyed positionally)
    so no data is silently dropped -- just flag the mismatch."""
    out = {}
    for i, val in enumerate(raw_values):
        period_label = periods[i] if i < len(periods) else f"column_{i+1}"
        out[period_label] = val * multiplier
    return out


def filename_data(pdf_path: str, bank_override: Optional[str] = None) -> dict:
    text = extract_text(pdf_path)
    filename = Path(pdf_path).name

    bank = detect_bank(text, filename, bank_override)
    filing_type = detect_filing_type(text)
    period_end = detect_period_end_date(text)

    return {
        "text": text,
        "source_file": filename,
        "bank": bank,
        "filing_type": filing_type,
        "period_end_date": period_end,
    }

def parse_filing(pdf_path: str, bank_override: Optional[str] = None) -> dict:
    filename_function = filename_data(pdf_path, bank_override)

    text = filename_function["text"]
    filename = filename_function["source_file"]

    bank = filename_function["bank"]
    filing_type = filename_function["filing_type"]
    period_end = filename_function["period_end_date"]

    bs_section = find_section(text, BALANCE_SHEET_HEADERS)
    is_section = find_section(text, INCOME_STATEMENT_HEADERS)

    warnings = []
    data = {}

    if bs_section:
        bs_periods = detect_periods("\n".join(bs_section.splitlines()[:6]))
        bs_units = detect_units(bs_section)
        bs_mult = UNIT_MULTIPLIER[bs_units]
        bs_items = extract_line_items(
            bs_section,
            [s for s in LINE_ITEMS if s.key in (
                "total_assets", "stockholders_equity", "total_deposits", "accumulated_oci"
            )],
        )
        for key, info in bs_items.items():
            data[key] = {
                "label_matched": info["label_matched"],
                "units": "USD",
                "reported_scale": bs_units,
                "values_by_period": build_period_value_map(info["raw_values"], bs_periods, bs_mult),
            }
        for key in ("total_assets", "stockholders_equity", "total_deposits", "accumulated_oci"):
            if key not in data:
                warnings.append(f"Could not locate line item '{key}' in balance sheet section")

        htm_items = extract_htm_securities(bs_section, bs_periods)
        for key, info in htm_items.items():
            data[key] = {
                "label_matched": info["label_matched"],
                "units": "USD",
                "reported_scale": bs_units,
                "values_by_period": build_period_value_map(info["raw_values"], bs_periods, bs_mult),
            }
        if not htm_items:
            warnings.append("Could not locate held-to-maturity securities disclosure in balance sheet section")
    else:
        warnings.append("Balance sheet / statement of financial condition section not found")

    if is_section:
        is_periods = detect_periods("\n".join(is_section.splitlines()[:6]))
        is_units = detect_units(is_section)
        is_mult = UNIT_MULTIPLIER[is_units]
        is_items = extract_line_items(
            is_section,
            [s for s in LINE_ITEMS if s.key in (
                "net_income", "noninterest_expense", "interest_income",
                "noninterest_income", "net_interest_income", "interest_expense_deposits",
            )],
        )
        for key, info in is_items.items():
            data[key] = {
                "label_matched": info["label_matched"],
                "units": "USD",
                "reported_scale": is_units,
                "values_by_period": build_period_value_map(info["raw_values"], is_periods, is_mult),
            }
        for key in ("net_income", "noninterest_expense", "interest_income",
                    "noninterest_income", "net_interest_income", "interest_expense_deposits"):
            if key not in data:
                warnings.append(f"Could not locate line item '{key}' in income statement section")
    else:
        warnings.append("Income statement section not found")

    # Derived metric: Interest Income + Noninterest Income, computed per
    # matching period label where both are available.
    if "interest_income" in data and "noninterest_income" in data:
        combined = {}
        ii = data["interest_income"]["values_by_period"]
        ni = data["noninterest_income"]["values_by_period"]
        for period in ii:
            if period in ni:
                combined[period] = ii[period] + ni[period]
        data["interest_income_plus_noninterest_income"] = {
            "label_matched": "derived: Total Interest Income + Total Noninterest Income",
            "units": "USD",
            "reported_scale": "derived",
            "values_by_period": combined,
        }
    else:
        warnings.append(
            "Could not compute 'Interest Income + Noninterest Income' "
            "(one or both source line items missing)"
        )

    return {
        "source_file": filename,
        "bank": bank,
        "filing_type": filing_type,
        "period_end_date": period_end,
        "data": data,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract key financials from bank 10-K/10-Q PDFs into JSON.")
    ap.add_argument("pdfs", nargs="+", help="Path(s) to 10-K/10-Q PDF filings")
    ap.add_argument("--bank", default=None, help="Override bank name detection (applies to all input files)")
    ap.add_argument("-o", "--output", default=None,
                     help="Output JSON path. If omitted, a name is generated from the "
                          "bank, filing type, and period(s) covered (e.g. "
                          "parsed_financials_Signature_Bank_10-K_2021.json).")
    args = ap.parse_args()

    results = []
    for pdf in args.pdfs:
        print(f"Parsing {pdf} ...", file=sys.stderr)
        result = parse_filing(pdf, bank_override=args.bank)
        results.append(result)
        if result["warnings"]:
            for w in result["warnings"]:
                print(f"  WARNING: {w}", file=sys.stderr)

    # The output filename is generated after parsing (not before, from
    # --bank alone) because it needs each filing's actually-detected
    # filing_type and period_end_date, not just the bank override.
    output_path = args.output or default_output_filename(results, args.bank)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Wrote {len(results)} filing(s) to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
