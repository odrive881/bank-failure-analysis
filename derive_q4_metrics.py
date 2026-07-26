"""Derive Q4 quarterly metrics as annual (10-K) minus Q1+Q2+Q3 (10-Q).

No bank files a 10-Q for Q4 -- it's only implicitly covered by the annual
10-K. For flow (income-statement) metrics, Q4 = FY - (Q1+Q2+Q3). For stock
(balance-sheet) metrics, Q4 *is* the FY year-end snapshot, not a subtraction.

Reads data/processed/<TICKER>_*.json (annual) and
data/processed/10-Q/<TICKER>/<TICKER>_<year>_10-Q.json (quarterly, Q1-Q3
only), and rewrites each quarterly file with an added "Q4" key.

Usage:
    python derive_q4_metrics.py
"""

import json
from pathlib import Path

from financial_data_standardization_modified import FLOW_METRICS, METRICS, quarter_end_date

ROOT = Path(__file__).resolve().parent
PROCESSED_DIR = ROOT / "data/processed"
QUARTERLY_DIR = PROCESSED_DIR / "10-Q"

STOCK_METRICS = set(METRICS) - FLOW_METRICS


def find_annual_file(ticker: str) -> Path | None:
    matches = sorted(PROCESSED_DIR.glob(f"{ticker}_*.json"))
    return matches[0] if matches else None


def derive_q4_metric(
    metric: str, year: int, annual: dict | None, q1: dict | None, q2: dict | None, q3: dict | None
) -> dict | None:
    """Derive one Q4 metric dict, or None if any required input is missing."""
    if annual is None or annual.get("value_usd") is None:
        return None

    if metric in STOCK_METRICS:
        value_usd = annual["value_usd"]
        calculation = "FY value (point-in-time balance)"
    else:
        parts = (q1, q2, q3)
        if any(part is None or part.get("value_usd") is None for part in parts):
            return None
        value_usd = annual["value_usd"] - sum(part["value_usd"] for part in parts)
        calculation = "FY - (Q1 + Q2 + Q3)"

    return {
        "value_usd": value_usd,
        "concept": "calculated",
        "end_date": quarter_end_date(year, "Q4"),
        "filed": "calculated",
        "accession_number": "calculated",
        "calculation": calculation,
    }


def is_pdf_derived(quarterly_metrics: dict) -> bool:
    """A PDF-derived (FRCB/SBNY) quarterly file carries source_label on every metric dict."""
    for quarter_metrics in quarterly_metrics.values():
        for metric_dict in quarter_metrics.values():
            if metric_dict is not None:
                return "source_label" in metric_dict
    return False


def build_q4(year: int, annual_metrics: dict, quarterly_metrics: dict) -> dict:
    q1, q2, q3 = quarterly_metrics["Q1"], quarterly_metrics["Q2"], quarterly_metrics["Q3"]
    pdf_derived = is_pdf_derived(quarterly_metrics)

    q4: dict = {}
    for metric in METRICS:
        result = derive_q4_metric(metric, year, annual_metrics.get(metric), q1.get(metric), q2.get(metric), q3.get(metric))
        if result is not None and pdf_derived:
            result["source_label"] = "calculated"
        q4[metric] = result

    interest = q4["interest_income"]
    noninterest = q4["noninterest_income"]
    q4["interest_income_plus_noninterest_income"] = (
        {"value_usd": interest["value_usd"] + noninterest["value_usd"],
         "calculation": "interest_income + noninterest_income"}
        if interest and noninterest else None
    )
    return q4


def warn_on_missing_q4(ticker: str, year: int, q4: dict) -> None:
    for metric, value in q4.items():
        if value is None:
            print(f"  WARNING: could not derive Q4 '{metric}' for {ticker} {year} "
                  f"-- a required Q1/Q2/Q3/annual input is missing.")


def process_ticker(ticker_dir: Path) -> None:
    ticker = ticker_dir.name
    annual_file = find_annual_file(ticker)
    if annual_file is None:
        print(f"  skipping {ticker} (no annual file found at data/processed/{ticker}_*.json)")
        return
    annual_data = json.loads(annual_file.read_text(encoding="utf-8"))
    annual_metrics = annual_data.get("annual_metrics", {})

    for quarterly_file in sorted(ticker_dir.glob(f"{ticker}_*_10-Q.json")):
        year = int(quarterly_file.stem.split("_")[1])
        if str(year) not in annual_metrics:
            print(f"  skipping {quarterly_file.relative_to(ROOT)} (no {year} annual data for {ticker})")
            continue

        data = json.loads(quarterly_file.read_text(encoding="utf-8"))
        quarterly_metrics = data["quarterly_metrics"]
        q4 = build_q4(year, annual_metrics[str(year)], quarterly_metrics)
        quarterly_metrics["Q4"] = q4
        quarterly_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote Q4 to {quarterly_file.relative_to(ROOT)}")
        warn_on_missing_q4(ticker, year, q4)


def main() -> None:
    for ticker_dir in sorted(p for p in QUARTERLY_DIR.iterdir() if p.is_dir()):
        process_ticker(ticker_dir)


if __name__ == "__main__":
    main()
