# Megabanks vs Regional Banks

A comparative financial-health analysis of six U.S. bank holding companies across 2020–2023:

- **Megabanks:** JPMorgan Chase (JPM), Bank of America (BAC)
- **Resilient regional:** PNC Financial Services (PNC)
- **Failed regionals:** SVB Financial/Silicon Valley Bank (SIVB), First Republic Bank (FRCB), Signature Bank (SBNY)

The goal is to see whether the failed regionals showed measurable pre-2023 stress — margin compression, worsening efficiency, mounting unrealized bond losses relative to capital — that the megabanks and PNC didn't, using ROE, ROA, Net Interest Margin (NIM), Efficiency Ratio (ER), and AOCI-as-%-of-equity as the core metrics.

All underlying data is public: SEC EDGAR filings/XBRL company facts and FDIC Call Report / bank-failure records.

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

This executes four stages in order:

1. **Raw data pull** (`companyfacts_data_pull.py`) — pulls SEC EDGAR XBRL company facts (JPM, BAC, PNC, SIVB), FDIC Call Report financials (all 6 banks), and FDIC failure records (the 3 failed banks) into `data/raw/`. Already-downloaded files are skipped.
2. **PDF parsing** (`pdf_parser/bank_10k_parser_modified.py`) — First Republic and Signature Bank aren't cleanly available via EDGAR, so their 10-K and 10-Q PDFs (`data/PDF raw data/`) are parsed directly into the same raw schema. Already-parsed PDFs are skipped.
3. **Standardization** (`financial_data_standardization_modified.py`) — normalizes both EDGAR and PDF-derived data into one common annual metric schema, written to `data/processed/`.
4. **Ingestion** (`processed_data_ingestion.py`) — flattens all processed per-bank JSON into one long-format dataframe, saved to `data/processed/processed_dataframes/DataframeLong.parquet`.

## Analysis

`data_analysis.ipynb` picks up from the parquet file: cleans and reshapes the data, independently recalculates ROA/ROE as a cross-check against FDIC Call Report figures, assembles a wide `master` dataframe (one row per bank-year, cohort-labeled) saved to `data/master_dataframe.csv`, and charts ROE, ROA, NIM, Efficiency Ratio, and AOCI-normalized trends by cohort. This notebook is a work in progress and will keep expanding.

## License

MIT — see [LICENSE](LICENSE).
