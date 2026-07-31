
"""Extract comparable annual bank metrics from SEC Company Facts JSON files.

Example:
    python financial_data_standardization.py data/raw/edgar_companyfacts/BANK_TICKER.json \
        --output data/processed/BANK_TICKER_2020_2023.json

    # Or process every Company Facts file in a folder:
    python financial_data_standardization.py data/raw/edgar_companyfacts \
        --output data/processed/banks_2020_2023.json

    # PDF-derived 10-K JSON records:
    python financial_data_standardization.py data/raw/edgar_companyfacts/Bank_name --output data/processed/Bank_name_2020_2022.json

    # PDF-derived 10-Q JSON records; writes one file per reporting year:
    python financial_data_standardization.py data/raw/edgar_companyfacts/FRB/10-Q --quarterly




The script intentionally retains the SEC concept used for each value.  This makes
company-specific taxonomy differences auditable instead of silently mixing them.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any


YEARS = (2020, 2021, 2022, 2023)

# EDGAR-sourced quarterly (10-Q) extraction only targets these years -- matches
# the range already established by the FRCB/SBNY PDF-derived quarterly files.
QUARTERLY_YEARS = (2021, 2022, 2023)

# Ordered fallbacks.  Add company-specific concepts here if a bank reports a
# metric under an extension taxonomy rather than a US-GAAP concept.
METRICS: dict[str, list[str]] = {
    "total_assets": ["us-gaap:Assets"],
    "net_income": ["us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss"],
    "stockholders_equity": ["us-gaap:StockholdersEquity"],
    "noninterest_expense": ["us-gaap:NoninterestExpense"],
    # JPM does not tag InterestAndDividendIncomeOperating at all; it uses
    # InterestIncomeOperating instead. Confirmed live against JPM's raw
    # companyfacts data (FY2020-2023 values are consistent with JPM's actual
    # total interest income), and against the fact that JPM's interest_income
    # was null in every already-shipped annual file before this fallback was
    # added.
    "interest_income": [
        "us-gaap:InterestAndDividendIncomeOperating",
        "us-gaap:InterestIncomeOperating",
    ],
    "noninterest_income": ["us-gaap:NoninterestIncome"],
    "total_deposits_consolidated": ["us-gaap:Deposits"],
    "accumulated_other_comprehensive_income_loss": [
        "us-gaap:AccumulatedOtherComprehensiveIncomeLossNetOfTax"
    ],
    # Net interest income (interest income less interest expense, before
    # provision for credit losses). Most bank holding companies use
    # InterestIncomeExpenseNet; InterestIncomeExpenseOperatingNet is kept as
    # a fallback for filers (e.g. certain REITs/thrifts) that tag it there
    # instead. Unverified against a live SEC response as of this edit --
    # confirm on your first real run (see note below extract_company).
    "net_interest_income": [
        "us-gaap:InterestIncomeExpenseNet",
        "us-gaap:InterestIncomeExpenseOperatingNet",
    ],
    # Interest expense specifically on deposits (excludes borrowings), used to
    # compute a deposit-specific cost-of-funds / deposit-beta metric rather
    # than a blended one. Confirmed present for JPM/BAC/PNC/SIVB.
    "interest_expense_deposits": ["us-gaap:InterestExpenseDeposits"],
}

FLOW_METRICS = {
    "net_income", "noninterest_expense", "interest_income", "noninterest_income",
    "net_interest_income", "interest_expense_deposits",
}

# Keys used by the PDF parser are intentionally mapped to the same output
# metric names as Company Facts.  New PDF parser keys can be added here later.
#
# NOTE on net_interest_income: this maps to a "net_interest_income" key in
# the upstream PDF-parser JSON, matching a "Net interest income" label. As of
# this edit, the raw parsed files already on hand (e.g. Signature Bank,
# First Republic) do NOT contain this key -- the upstream PDF extraction
# step has not been taught to capture that label yet. Until it is, this
# metric will silently come back null for every PDF-derived record, exactly
# like the earlier total_assets/stockholders_equity nulls you caught. This
# is expected, not a bug in this script -- the fix belongs in the upstream
# PDF parser, not here.
PDF_METRICS = {
    "total_assets": "total_assets",
    "net_income": "net_income",
    "stockholders_equity": "stockholders_equity",
    "noninterest_expense": "noninterest_expense",
    "interest_income": "interest_income",
    "noninterest_income": "noninterest_income",
    "total_deposits_consolidated": "total_deposits",
    "accumulated_other_comprehensive_income_loss": "accumulated_oci",
    "net_interest_income": "net_interest_income",
    "interest_expense_deposits": "interest_expense_deposits",
}

# A source row contains all comparative-period columns.  Processed output is
# annual, so retain only the first (current-year) displayed amount alongside
# the label. Parentheses denote negative values in financial statements.
SOURCE_VALUE_RE = re.compile(
    r"\(\$?\s?[\d,]+(?:\.\d+)?\)|\$?\s?[\d,]+(?:\.\d+)?"
)


def concept_fact(facts: dict[str, Any], concept: str) -> dict[str, Any] | None:
    """Return a fact definition for a ``taxonomy:tag`` concept."""
    taxonomy, tag = concept.split(":", 1)
    return facts.get(taxonomy, {}).get(tag)


def best_observation(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Among duplicate observations (comparative columns, later filings), prefer
    the most recently filed one."""
    if not observations:
        return None
    return max(observations, key=lambda item: (item.get("filed", ""), item.get("accn", "")))


def annual_observation(fact: dict[str, Any], year: int, is_flow: bool) -> dict[str, Any] | None:
    """Choose the best USD annual 10-K observation ending in ``year``.

    SEC companyfacts includes duplicate observations from comparative columns and
    later filings.  Prefer an FY 10-K, then the most recently filed observation.
    A flow must cover approximately one year; this prevents YTD 10-Q values from
    being used as annual income-statement values.
    """
    candidates: list[dict[str, Any]] = []
    for unit, observations in fact.get("units", {}).items():
        if unit != "USD":
            continue
        for obs in observations:
            if (obs.get("form") != "10-K" or obs.get("fp") != "FY"
                    or obs.get("fy") != year):
                continue
            if not str(obs.get("end", "")).startswith(str(year)):
                continue
            if is_flow:
                start = obs.get("start")
                if not start or not str(start).startswith(str(year)):
                    continue
                # A normal annual duration is 350--380 days. This also supports
                # non-calendar fiscal years without accepting quarterly periods.
                duration = (date.fromisoformat(obs["end"]) - date.fromisoformat(start)).days
                if not 350 <= duration <= 380:
                    continue
            candidates.append(obs)
    return best_observation(candidates)


def extract_metric(facts: dict[str, Any], metric: str, year: int) -> dict[str, Any] | None:
    for concept in METRICS[metric]:
        fact = concept_fact(facts, concept)
        if fact:
            observation = annual_observation(fact, year, metric in FLOW_METRICS)
            if observation:
                return {
                    "value_usd": observation["val"],
                    "concept": concept,
                    "end_date": observation["end"],
                    "filed": observation["filed"],
                    "accession_number": observation["accn"],
                }
    return None


def extract_company(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    facts = raw["facts"]
    result: dict[str, Any] = {
        "cik": str(raw.get("cik", "")).zfill(10),
        "company_name": raw.get("entityName"),
        "source_file": path.name,
        "currency": "USD",
        "annual_metrics": {},
    }
    for year in YEARS:
        metrics = {metric: extract_metric(facts, metric, year) for metric in METRICS}
        interest = metrics["interest_income"]
        noninterest = metrics["noninterest_income"]
        metrics["interest_income_plus_noninterest_income"] = (
            {"value_usd": interest["value_usd"] + noninterest["value_usd"],
             "calculation": "interest_income + noninterest_income"}
            if interest and noninterest else None
        )
        result["annual_metrics"][str(year)] = metrics
    return result


def is_edgar_companyfacts(path: Path) -> bool:
    """Identify a raw SEC Company Facts JSON file (as opposed to PDF-parser output)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and isinstance(data.get("facts"), dict)


def quarter_candidates(fact: dict[str, Any], year: int, quarter: str) -> list[dict[str, Any]]:
    """USD observations from a 10-Q filing for the given fiscal year/quarter."""
    candidates: list[dict[str, Any]] = []
    for unit, observations in fact.get("units", {}).items():
        if unit != "USD":
            continue
        for obs in observations:
            if (obs.get("form") != "10-Q" or obs.get("fp") != quarter
                    or obs.get("fy") != year):
                continue
            if not str(obs.get("end", "")).startswith(str(year)):
                continue
            candidates.append(obs)
    return candidates


def _duration_days(obs: dict[str, Any]) -> int | None:
    start = obs.get("start")
    if not start:
        return None
    return (date.fromisoformat(obs["end"]) - date.fromisoformat(start)).days


def discrete_quarter_observation(fact: dict[str, Any], year: int, quarter: str) -> dict[str, Any] | None:
    """A single-quarter (~90-day) duration observation -- never a YTD-cumulative one."""
    return best_observation([
        obs for obs in quarter_candidates(fact, year, quarter)
        if (duration := _duration_days(obs)) is not None and 75 <= duration <= 100
    ])


def ytd_quarter_observation(fact: dict[str, Any], year: int, quarter: str) -> dict[str, Any] | None:
    """The YTD-cumulative-through-this-quarter observation (duration > 100 days).

    For Q1, YTD(Q1) == discrete Q1 (both ~90 days), so this deliberately returns
    None for Q1 -- callers needing YTD(Q1) should use
    discrete_quarter_observation(fact, year, "Q1") instead.
    """
    return best_observation([
        obs for obs in quarter_candidates(fact, year, quarter)
        if (duration := _duration_days(obs)) is not None and duration > 100
    ])


def quarterly_flow_observation(
    fact: dict[str, Any], year: int, quarter: str
) -> tuple[dict[str, Any] | None, bool]:
    """Resolve a flow metric for one quarter. Returns ``(observation, used_ytd_fallback)``.

    Prefers a discrete single-quarter observation (present for BAC/JPM/PNC/SIVB
    across 2021-2023 for every flow concept used here). Falls back to
    YTD(quarter) - YTD(previous quarter) only if no discrete observation exists.
    The Q3 fallback must subtract genuine YTD(Q2) (~180 days), not discrete Q2
    (~90 days) -- these are different numbers.
    """
    discrete = discrete_quarter_observation(fact, year, quarter)
    if discrete:
        return discrete, False
    current_ytd = ytd_quarter_observation(fact, year, quarter)
    if not current_ytd:
        return None, False
    if quarter == "Q1":
        return current_ytd, False  # YTD(Q1) IS the discrete Q1 value; no real subtraction
    if quarter == "Q2":
        prior = discrete_quarter_observation(fact, year, "Q1")  # YTD(Q1) == discrete Q1
    elif quarter == "Q3":
        prior = ytd_quarter_observation(fact, year, "Q2")  # must be genuine YTD(Q2)
    else:
        prior = None
    if not prior:
        return None, False
    synthesized = {
        "val": current_ytd["val"] - prior["val"],
        "end": current_ytd["end"],
        "filed": current_ytd.get("filed"),
        "accn": current_ytd.get("accn"),
    }
    return synthesized, True


def extract_quarterly_metric_value(
    facts: dict[str, Any], metric: str, year: int, quarter: str
) -> dict[str, Any] | None:
    is_flow = metric in FLOW_METRICS
    for concept in METRICS[metric]:
        fact = concept_fact(facts, concept)
        if not fact:
            continue
        if is_flow:
            observation, used_fallback = quarterly_flow_observation(fact, year, quarter)
        else:
            observation, used_fallback = best_observation(quarter_candidates(fact, year, quarter)), False
        if observation:
            result = {
                "value_usd": observation["val"],
                "concept": concept,
                "end_date": observation["end"],
                "filed": observation.get("filed"),
                "accession_number": observation.get("accn"),
            }
            if used_fallback:
                prior_quarter = {"Q2": "Q1", "Q3": "Q2"}[quarter]
                result["calculation"] = f"YTD({quarter}) - YTD({prior_quarter})"
            return result
    return None


def extract_company_quarterly(path: Path, years: tuple[int, ...] = QUARTERLY_YEARS) -> dict[int, dict[str, Any]]:
    """Extract EDGAR-sourced quarterly (10-Q) metrics, one output record per year.

    Only Q1-Q3 are ever populated: no 10-Q is filed for Q4, which is instead
    covered by the 10-K (mirrors the existing PDF-derived quarterly output).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    facts = raw["facts"]
    outputs: dict[int, dict[str, Any]] = {}
    for year in years:
        quarterly_metrics: dict[str, Any] = {}
        for quarter in ("Q1", "Q2", "Q3"):
            metrics = {
                metric: extract_quarterly_metric_value(facts, metric, year, quarter)
                for metric in METRICS
            }
            interest = metrics["interest_income"]
            noninterest = metrics["noninterest_income"]
            metrics["interest_income_plus_noninterest_income"] = (
                {"value_usd": interest["value_usd"] + noninterest["value_usd"],
                 "calculation": "interest_income + noninterest_income"}
                if interest and noninterest else None
            )
            quarterly_metrics[quarter] = metrics
        has_data = any(value is not None for q in quarterly_metrics.values() for value in q.values())
        if has_data:
            outputs[year] = {
                "cik": str(raw.get("cik", "")).zfill(10),
                "company_name": raw.get("entityName"),
                "source_file": path.name,
                "currency": "USD",
                "quarterly_metrics": quarterly_metrics,
            }
    return outputs


def is_pdf_derived_10k(path: Path) -> bool:
    """Identify a JSON file emitted by the local PDF financial parser."""
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(records, list)
        and bool(records)
        and isinstance(records[0], dict)
        and records[0].get("filing_type") == "10-K"
        and isinstance(records[0].get("data"), dict)
    )


def is_pdf_derived_10q(path: Path) -> bool:
    """Identify a JSON file emitted by the local PDF parser for a 10-Q."""
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(records, list)
        and bool(records)
        and isinstance(records[0], dict)
        and records[0].get("filing_type") == "10-Q"
        and isinstance(records[0].get("data"), dict)
    )


def comparative_end_date(record: dict[str, Any], year: int) -> str | None:
    """Return the period end date for a possibly-comparative (non-current) year.

    A filing's own ``period_end_date`` only describes its current reporting
    period. When a value is read from an older comparative column (e.g. the
    2020 column inside a 2022 10-K), stamping it with the filing's own date
    silently mislabels it. Substitute the filing's own year with the
    requested year, assuming a consistent (typically calendar) fiscal
    year-end, which holds for every bank in this dataset.
    """
    period_end = record.get("period_end_date")
    if not period_end:
        return None
    filing_year_match = re.search(r"\b(20\d{2})\b", period_end)
    if not filing_year_match:
        return period_end
    return period_end.replace(filing_year_match.group(1), str(year))


def pdf_metric_value(record: dict[str, Any], pdf_key: str, year: int) -> dict[str, Any] | None:
    """Convert one PDF-parser metric into the common output metric schema."""
    source = record["data"].get(pdf_key)
    if not source or str(year) not in source.get("values_by_period", {}):
        return None
    source_label = source.get("label_matched")
    if source_label:
        first_value = SOURCE_VALUE_RE.search(source_label)
        if first_value:
            source_label = source_label[:first_value.end()].rstrip()
    return {
        "value_usd": source["values_by_period"][str(year)],
        "concept": f"pdf-derived:{pdf_key}",
        "end_date": comparative_end_date(record, year),
        "filed": None,
        "accession_number": None,
        "source_label": source_label,
    }


def pdf_quarterly_metric_value(record: dict[str, Any], pdf_key: str, year: int) -> dict[str, Any] | None:
    """Extract a 10-Q metric, tolerating parser outputs keyed by full dates.

    A 10-Q displays the current period first. Older parser outputs sometimes
    label that column ``December 31,\n2022`` instead of simply ``2022``.
    """
    metric = pdf_metric_value(record, pdf_key, year)
    if metric:
        return metric
    source = record["data"].get(pdf_key)
    values = source.get("values_by_period", {}) if source else {}
    if not values:
        return None
    source_label = source.get("label_matched")
    if source_label:
        first_value = SOURCE_VALUE_RE.search(source_label)
        if first_value:
            source_label = source_label[:first_value.end()].rstrip()
    return {
        "value_usd": next(iter(values.values())),
        "concept": f"pdf-derived:{pdf_key}",
        "end_date": record.get("period_end_date"),
        "filed": None,
        "accession_number": None,
        "source_label": source_label,
    }


def filing_reporting_year(record: dict[str, Any]) -> int | None:
    """The fiscal year a 10-K record's own (current-period) column covers."""
    match = re.search(r"\b(20\d{2})\b", record.get("period_end_date", ""))
    return int(match.group(1)) if match else None


def extract_pdf_10k_company(paths: list[Path]) -> dict[str, Any]:
    """Extract annual metrics from PDF-derived 10-K JSON files only."""
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        for record in json.loads(path.read_text(encoding="utf-8")):
            if record.get("filing_type") == "10-K":
                records.append((path, record))
    if not records:
        raise ValueError("No PDF-derived 10-K records were found.")

    years = sorted({
        int(year)
        for _, record in records
        for metric in record["data"].values()
        for year in metric.get("values_by_period", {})
        if year in {str(y) for y in YEARS}
    })
    company_name = records[0][1].get("bank")
    result: dict[str, Any] = {
        "cik": None,
        "company_name": company_name,
        "source_file": [path.name for path, _ in records],
        "currency": "USD",
        "annual_metrics": {},
    }
    for year in years:
        # Rank candidate filings by how close their own reporting year is to
        # the target year (closest first); a filing whose own year exactly
        # matches sorts first, then its immediate successor filing (where the
        # target year appears as the prior-year comparative column), etc.
        ranked = sorted(
            records,
            key=lambda item: (
                float("inf") if filing_reporting_year(item[1]) is None
                else abs(filing_reporting_year(item[1]) - year)
            ),
        )
        metrics: dict[str, Any] = {}
        for standard_key, pdf_key in PDF_METRICS.items():
            metrics[standard_key] = None
            for _, candidate in ranked:
                value = pdf_metric_value(candidate, pdf_key, year)
                if value:
                    metrics[standard_key] = value
                    break
        interest = metrics["interest_income"]
        noninterest = metrics["noninterest_income"]
        metrics["interest_income_plus_noninterest_income"] = (
            {"value_usd": interest["value_usd"] + noninterest["value_usd"],
             "calculation": "interest_income + noninterest_income"}
            if interest and noninterest else None
        )
        result["annual_metrics"][str(year)] = metrics
    return result


def filing_quarter(record: dict[str, Any]) -> str | None:
    """Read a Q1--Q4 marker from the original PDF filename."""
    filename = record.get("source_file", "")
    for pattern in (r"(?:^|[_\-])Q([1-4])(?:[_\-.]|$)", r"(?:^|[_\-])([1-4])Q(?:[_\-.]|$)"):
        match = re.search(pattern, filename, re.IGNORECASE)
        if match:
            return f"Q{match.group(1)}"
    return None


def filing_year(record: dict[str, Any]) -> int | None:
    """Infer the current reporting year from the filing date, data, or name."""
    date_year = re.search(r"\b(20\d{2})\b", record.get("period_end_date") or "")
    if date_year:
        return int(date_year.group(1))
    for metric in record.get("data", {}).values():
        for period in metric.get("values_by_period", {}):
            if re.fullmatch(r"20\d{2}", period):
                return int(period)
    years = re.findall(r"20\d{2}", record.get("source_file", ""))
    return int(years[-1]) if years else None


def quarter_end_date(year: int, quarter: str) -> str:
    return {
        "Q1": f"March 31, {year}", "Q2": f"June 30, {year}",
        "Q3": f"September 30, {year}", "Q4": f"December 31, {year}",
    }[quarter]


def extract_pdf_10q_companies(paths: list[Path]) -> dict[int, dict[str, Any]]:
    """Standardize PDF-derived 10-Q filings into one quarter-organized record per year."""
    filings: list[tuple[Path, dict[str, Any], int, str]] = []
    for path in paths:
        for record in json.loads(path.read_text(encoding="utf-8")):
            if record.get("filing_type") != "10-Q":
                continue
            year, quarter = filing_year(record), filing_quarter(record)
            if year is not None and quarter is not None:
                filings.append((path, record, year, quarter))
    if not filings:
        raise ValueError("No PDF-derived 10-Q records with identifiable years and quarters were found.")

    outputs: dict[int, dict[str, Any]] = {}
    for year in sorted({year for _, _, year, _ in filings}):
        year_filings = [filing for filing in filings if filing[2] == year]
        result: dict[str, Any] = {
            "cik": None,
            "company_name": year_filings[0][1].get("bank"),
            "source_file": [path.name for path, _, _, _ in year_filings],
            "currency": "USD",
            "quarterly_metrics": {},
        }
        for _, record, _, quarter in sorted(year_filings, key=lambda item: item[3]):
            metrics = {
                standard_key: pdf_quarterly_metric_value(record, pdf_key, year)
                for standard_key, pdf_key in PDF_METRICS.items()
            }
            end_date = record.get("period_end_date") or quarter_end_date(year, quarter)
            for metric in metrics.values():
                if metric:
                    metric["end_date"] = end_date
            interest, noninterest = metrics["interest_income"], metrics["noninterest_income"]
            metrics["interest_income_plus_noninterest_income"] = (
                {"value_usd": interest["value_usd"] + noninterest["value_usd"],
                 "calculation": "interest_income + noninterest_income"}
                if interest and noninterest else None
            )
            result["quarterly_metrics"][quarter] = metrics
        outputs[year] = result
    return outputs


def warn_on_missing_metrics(output: dict[str, Any]) -> None:
    """Print a warning for any metric that is null across every processed year.

    A metric missing every year usually means the wrong concept/label is being
    looked for (as opposed to a metric that's genuinely absent in one or two
    years), so it's worth surfacing immediately rather than discovering it
    later while building the analysis dataframe.
    """
    annual = output.get("annual_metrics") or {}
    quarterly = output.get("quarterly_metrics") or {}
    periods = annual or quarterly
    if not periods:
        return
    all_metric_keys: set[str] = set()
    for period_metrics in periods.values():
        all_metric_keys.update(period_metrics.keys())
    company = output.get("company_name", "<unknown company>")
    for metric in sorted(all_metric_keys):
        if metric == "interest_income_plus_noninterest_income":
            continue  # derived; a metric-level warning elsewhere already covers its inputs
        if all(period_metrics.get(metric) is None for period_metrics in periods.values()):
            print(f"  WARNING: '{metric}' is null for every period in {company} -- "
                  f"check the concept/label mapping for this metric.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract annual SEC Company Facts bank metrics.")
    parser.add_argument("input", type=Path, help="A Company Facts JSON file or folder of JSON files.")
    parser.add_argument("--output", type=Path, help="Destination JSON file (or output directory with --quarterly).")
    parser.add_argument("--quarterly", action="store_true", help="Process PDF-derived 10-Q files and write one JSON file per year.")
    args = parser.parse_args()

    files = [args.input] if args.input.is_file() else sorted(args.input.glob("*.json"))
    if not files:
        raise SystemExit(f"No JSON files found at: {args.input}")
    if args.quarterly:
        pdf_10q_files = [file for file in files if is_pdf_derived_10q(file)]
        if pdf_10q_files:
            outputs = extract_pdf_10q_companies(pdf_10q_files)
            ticker = args.input.parent.name if args.input.name.lower() == "10-q" else args.input.name
        elif len(files) == 1 and is_edgar_companyfacts(files[0]):
            outputs = extract_company_quarterly(files[0])
            ticker = files[0].stem
        else:
            raise SystemExit(
                f"No recognized quarterly input (PDF-derived 10-Q or EDGAR companyfacts) "
                f"found at: {args.input}"
            )
        output_dir = args.output or Path("data/processed/10-Q") / ticker
        output_dir.mkdir(parents=True, exist_ok=True)
        for year, output in outputs.items():
            destination = output_dir / f"{ticker}_{year}_10-Q.json"
            destination.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Wrote {destination}")
            warn_on_missing_metrics(output)
        return
    if args.output is None:
        parser.error("--output is required unless --quarterly is used.")
    # Keep directory ingestion non-recursive: SBNY's future 10-Q inputs live in
    # a subfolder and must not be picked up until quarterly support is added.
    pdf_10k_files = [file for file in files if is_pdf_derived_10k(file)]
    if pdf_10k_files:
        output: Any = extract_pdf_10k_company(pdf_10k_files)
    else:
        output = extract_company(files[0]) if len(files) == 1 else [extract_company(file) for file in files]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for single_output in (output if isinstance(output, list) else [output]):
        warn_on_missing_metrics(single_output)


if __name__ == "__main__":
    main()
