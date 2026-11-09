# Megabanks vs Regional Banks

A light comparative analysis of the American banking sector leading up to the 2023 US Banking Crisis. The goal of the project being to see what metrics serve as the most plausible financial stress signals for the regional banks that failed. The cohorts analyzed include:

- **Megabanks:** JPMorgan Chase (JPM), Bank of America (BAC)
- **Resilient regional:** PNC Financial Services (PNC)
- **Failed regionals:** SVB Financial/Silicon Valley Bank (SIVB), First Republic Bank (FRCB), Signature Bank (SBNY)

All underlying data is public: SEC EDGAR filings/XBRL company facts and FDIC Call Report / bank-failure records 

## Setup

Requires Python 3.14 and, for the PDF parsing stage, [poppler-utils](https://poppler.freedesktop.org/) installed on your system (provides `pdftotext`; the parser falls back to `pdfplumber` automatically if it isn't found).

```
python -m venv .venv
.venv\Scripts\activate      # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
```

SEC EDGAR requires a contact email in every request's User-Agent header. Set one before running the raw data pull:

```
# PowerShell
$env:EDGAR_CONTACT_EMAIL = "you@example.com"

# bash
export EDGAR_CONTACT_EMAIL=you@example.com
```

See `.env.example` for reference (this project reads the variable straight from the environment, not from a `.env` file).

## Data pipeline

Run the whole thing with:

```
python run_pipeline.py
```

This executes five stages in order:

1. **Raw data pull** (`companyfacts_data_pull.py`) — pulls SEC EDGAR XBRL company facts (JPM, BAC, PNC, SIVB), FDIC Call Report financials (all 6 banks), FDIC failure records (the 3 failed banks), and the FRED effective federal funds rate (used later by the notebook's deposit-beta analysis) into `data/raw/`. Already-downloaded files are skipped.
2. **PDF parsing** (`pdf_parser/bank_10k_parser_modified.py`) — First Republic and Signature Bank aren't cleanly available via EDGAR, so their 10-K and 10-Q PDFs (`data/PDF raw data/`) are parsed directly into the same raw schema. Already-parsed PDFs are skipped.
3. **Standardization** (`financial_data_standardization_modified.py`) — normalizes both EDGAR and PDF-derived data into one common annual metric schema, written to `data/processed/`, and a quarterly (Q1-Q3) schema written to `data/processed/10-Q/`. No bank files a 10-Q for Q4, so Q1-Q3 is all that's directly parsed.
4. **Q4 derivation** (`derive_q4_metrics.py`) — fills in the missing Q4 for each bank-year: flow metrics (net income, interest/noninterest income and expense, net interest income) are computed as the annual 10-K value minus Q1+Q2+Q3; balance-sheet metrics (total assets, equity, deposits, AOCI) use the 10-K year-end value directly. Derived fields are marked `"calculated"` in place of the usual source provenance. Runs after standardization on every pipeline run, since standardization always regenerates the Q1-Q3 files from scratch.
5. **Ingestion** (`processed_data_ingestion.py`) — flattens all processed per-bank annual and quarterly JSON into one long-format dataframe (with a `quarter` column: `FY`, `Q1`-`Q4`), saved to `data/processed/processed_dataframes/DataframeLong.parquet`.

## Analysis

`data_analysis.ipynb` picks up from the parquet file and ends in a completed closing-summary section — it's no longer a bare work-in-progress, though see Limitations below before treating its conclusions as more than descriptive.

**Data prep & cross-checks.** Loads and reshapes the long-format parquet, independently recalculates ROA/ROE/Efficiency Ratio from the raw 10-K figures, ingests FDIC Call Report data per bank-quarter, and cross-checks the two sources — flagging bank-years where the ratio between them falls outside a 75%-115% band (e.g. SIVB and BAC, both attributable to accounting-recognition timing differences rather than a data bug). The results are pivoted into a wide `master` dataframe (one row per bank-year, cohort-labeled) and saved to `data/master_dataframe.csv`.

**Extended metric set.** Beyond the five headline metrics (ROE, ROA, NIM, Efficiency Ratio, AOCI-as-%-of-equity), the notebook derives and charts: uninsured-deposit share, loan-to-deposit ratio, HTM-securities unrealized-loss-as-%-of-equity, YoY deposit growth, deposit beta (cost-of-deposits sensitivity to the Fed Funds Rate), ROE/ER trend slopes (2020-2022) with significance, an eta-squared "gap vs. spread" cohort-separation score across 8 metrics, bootstrap 95%-confidence intervals on cohort-mean gaps, a quarterly deep-dive with bank-failure date markers, and a stock-price reaction study (via `yfinance`, with a manually-sourced CSV for SBNY since `yfinance` only has post-collapse data for it).

**Key findings.** ROA and ROE alone don't distinguish the failed cohort from the megabanks/PNC. NIM and Efficiency Ratio show a mild pre-2023 signal (margin compression, rising expense ratios) for the failed banks. The clearest separator is AOCI erosion combined with uninsured-deposit share — the eta-squared analysis confirms this pairing accounts for the most cross-cohort variance, and SIVB in particular shows severe HTM-unrealized-loss buildup from pre-hike-era purchases. The stock-price study finds the market didn't price in this stress in advance: prices held steady (SIVB's even grew) until the SIVB run triggered a rapid repricing across the failed cohort, while PNC's valuation stayed comparatively stable throughout.

**Limitations.** The sample is small and unbalanced by construction — 2 megabanks, 1 resilient regional, 3 failed regionals (~21 bank-years total) — so trend-slope p-values and cohort-gap estimates are descriptive, not hypothesis-test-grade evidence, and none of this should be read as predictive of bank failure. FY2023 data is absent for the three failed banks since they stopped filing after their collapse.

**Outputs.** `data/master_dataframe.csv` (the assembled wide dataframe) and `Data Graphs/` (PNG exports of the notebook's charts) are both checked into the repo. The notebook itself only calls `plt.show()` — the PNGs in `Data Graphs/` are exported manually from the rendered output, so re-running the notebook won't regenerate them automatically; re-export by hand if the underlying data or charts change.

## License

MIT — see [LICENSE](LICENSE).
