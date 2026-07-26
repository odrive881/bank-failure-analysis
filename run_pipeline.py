"""Run the full pre-notebook pipeline in one go: raw data pull, PDF parsing
for the two banks without EDGAR companyfacts, standardization, and long-format
parquet assembly. Stops short of data_analysis.ipynb, which is run separately.

Usage:
    python run_pipeline.py
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


def run(args, cwd=ROOT):
    subprocess.run(args, cwd=cwd, check=True)


def pull_raw_data():
    run([PYTHON, "companyfacts_data_pull.py"])


# (pdf_path, bank_name, output_path), mirroring pdf_parser/run formulas.txt
PDF_JOBS = [
    (
        "data/PDF raw data/FRB/2021/10-K/EFR33520227992-1_FRB_10_K_2021.pdf",
        "First Republic Bank",
        "data/raw/edgar_companyfacts/FRCB/parsed_financials_First_Republic_Bank_10-K_2021.json",
    ),
    (
        "data/PDF raw data/FRB/2022/10-K/EFR335202311265-1_FRB_10_K_2022.pdf",
        "First Republic Bank",
        "data/raw/edgar_companyfacts/FRCB/parsed_financials_First_Republic_Bank_10-K_2022.json",
    ),
    (
        "data/PDF raw data/SBNY/2021/10-K/EFR33520227994-1_SignatureBank_12.31.21_10K_FINAL__filing_copy_.pdf",
        "Signature Bank",
        "data/raw/edgar_companyfacts/SBNY/parsed_financials_Signature_Bank_10-K_2021.json",
    ),
    (
        "data/PDF raw data/SBNY/2022/10-K/EFR335202311266-1_SignatureBank_12.31.22_10K__FINAL__FILING_COPY_.pdf",
        "Signature Bank",
        "data/raw/edgar_companyfacts/SBNY/parsed_financials_Signature_Bank_10-K_2022.json",
    ),
]


def parse_pdfs():
    for pdf_path, bank_name, output_path in PDF_JOBS:
        if (ROOT / output_path).exists():
            print(f"  skipping {output_path} (already exists)")
            continue
        run([
            PYTHON, "pdf_parser/bank_10k_parser_modified.py",
            pdf_path, "--bank", bank_name, "-o", output_path,
        ])


QUARTERLY_PDF_ROOT = ROOT / "data/PDF raw data"

# ticker (as it appears under data/PDF raw data/) -> (bank name, raw-output ticker dir).
# First Republic's raw companyfacts folder is FRCB, not FRB -- matches PDF_JOBS above.
QUARTERLY_BANKS = {
    "FRB": ("First Republic Bank", "FRCB"),
    "SBNY": ("Signature Bank", "SBNY"),
}


def _file_hash(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


# Two filing-name conventions are in use across banks: FRB writes "..._Q1_2021...",
# SBNY writes "..._1Q_2021...". Mirrors filing_quarter() in
# financial_data_standardization_modified.py.
QUARTER_PATTERNS = (
    re.compile(r"(?:^|[_\-])Q([1-4])(?:[_\-.]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[_\-])([1-4])Q(?:[_\-.]|$)", re.IGNORECASE),
)


def _detect_quarter(filename: str):
    for pattern in QUARTER_PATTERNS:
        match = pattern.search(filename)
        if match:
            return match.group(1)
    return None


def discover_quarterly_pdfs():
    """Find every 10-Q PDF under data/PDF raw data/{TICKER}/{YEAR}/10-Q/, for all years
    present, deduplicated by content hash so a misfiled duplicate (the same filing copied
    into the wrong year's folder) isn't parsed twice under two different period labels."""
    seen_hashes = {}
    jobs = []
    for pdf_path in sorted(QUARTERLY_PDF_ROOT.glob("*/*/10-Q/*.pdf")):
        ticker = pdf_path.parents[2].name
        if ticker not in QUARTERLY_BANKS:
            continue

        digest = _file_hash(pdf_path)
        if digest in seen_hashes:
            print(f"  skipping {pdf_path.relative_to(ROOT)} "
                  f"(duplicate content of {seen_hashes[digest].relative_to(ROOT)})")
            continue
        seen_hashes[digest] = pdf_path

        quarter = _detect_quarter(pdf_path.name)
        if not quarter:
            print(f"  skipping {pdf_path.relative_to(ROOT)} (couldn't determine quarter from filename)")
            continue

        year = pdf_path.parents[1].name
        bank_name, raw_ticker = QUARTERLY_BANKS[ticker]
        bank_slug = bank_name.replace(" ", "_")
        output_path = (
            f"data/raw/edgar_companyfacts/{raw_ticker}/10-Q/"
            f"parsed_financials_{bank_slug}_10-Q_{year}_Q{quarter}.json"
        )
        jobs.append((str(pdf_path.relative_to(ROOT)), bank_name, output_path))
    return jobs


def parse_quarterly_pdfs():
    for pdf_path, bank_name, output_path in discover_quarterly_pdfs():
        if (ROOT / output_path).exists():
            print(f"  skipping {output_path} (already exists)")
            continue
        run([
            PYTHON, "pdf_parser/bank_10k_parser_modified.py",
            pdf_path, "--bank", bank_name, "-o", output_path,
        ])


# 6 standardization commands, mirroring "financial standardization run commands.txt"
STANDARDIZATION_JOBS = [
    ("data/raw/edgar_companyfacts/BAC.json", "data/processed/BAC_2020_2023.json"),
    ("data/raw/edgar_companyfacts/JPM.json", "data/processed/JPM_2020_2023.json"),
    ("data/raw/edgar_companyfacts/PNC.json", "data/processed/PNC_2020_2023.json"),
    ("data/raw/edgar_companyfacts/SIVB.json", "data/processed/SIVB_2020_2023.json"),
    ("data/raw/edgar_companyfacts/FRCB", "data/processed/FRCB_2020_2022.json"),
    ("data/raw/edgar_companyfacts/SBNY", "data/processed/SBNY_2020_2022.json"),
]


def standardize():
    for input_path, output_path in STANDARDIZATION_JOBS:
        run([
            PYTHON, "financial_data_standardization_modified.py",
            input_path, "--output", output_path,
        ])


QUARTERLY_STANDARDIZATION_JOBS = [
    ("data/raw/edgar_companyfacts/BAC.json", "data/processed/10-Q/BAC"),
    ("data/raw/edgar_companyfacts/JPM.json", "data/processed/10-Q/JPM"),
    ("data/raw/edgar_companyfacts/PNC.json", "data/processed/10-Q/PNC"),
    ("data/raw/edgar_companyfacts/SIVB.json", "data/processed/10-Q/SIVB"),
    ("data/raw/edgar_companyfacts/FRCB/10-Q", "data/processed/10-Q/FRCB"),
    ("data/raw/edgar_companyfacts/SBNY/10-Q", "data/processed/10-Q/SBNY"),
]


def standardize_quarterly():
    # Always regenerate (no skip-if-exists): this is a cheap JSON transform, and running it
    # is what keeps output current if financial_data_standardization_modified.py's own
    # extraction logic improves after raw 10-Q JSON already exists.
    for input_path, output_path in QUARTERLY_STANDARDIZATION_JOBS:
        if not (ROOT / input_path).exists():
            print(f"  skipping {input_path} (no 10-Q raw data found)")
            continue
        run([
            PYTHON, "financial_data_standardization_modified.py",
            input_path, "--quarterly", "--output", output_path,
        ])


def derive_q4():
    run([PYTHON, "derive_q4_metrics.py"])


def ingest():
    run([PYTHON, "processed_data_ingestion.py"])


STAGES = [
    ("Pulling raw data (EDGAR companyfacts, FDIC call reports, FDIC failures)", pull_raw_data),
    ("Parsing FRCB/SBNY 10-K PDFs", parse_pdfs),
    ("Parsing FRCB/SBNY 10-Q PDFs", parse_quarterly_pdfs),
    ("Standardizing raw data into annual metrics", standardize),
    ("Standardizing raw data into quarterly metrics", standardize_quarterly),
    ("Deriving Q4 metrics (annual 10-K minus 10-Q Q1-Q3)", derive_q4),
    ("Assembling long-format parquet", ingest),
]


def main():
    total = len(STAGES)
    for i, (description, stage_func) in enumerate(STAGES, start=1):
        print(f"[{i}/{total}] {description}...")
        try:
            stage_func()
        except subprocess.CalledProcessError:
            print(f"Stage {i} ({description}) failed. Stopping pipeline.", file=sys.stderr)
            raise
    print("Pipeline complete.")


if __name__ == "__main__":
    main()
