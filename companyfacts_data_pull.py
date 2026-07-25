import json
import os
import time

import requests

# ==========================================================
# Configuration
# ==========================================================

headers = {
    "User-Agent": f"DataAnalysisProject {os.environ.get('EDGAR_CONTACT_EMAIL', 'you@example.com')}"
}

COMPANYFACTS_DIR = "data/raw/edgar_companyfacts"
CALL_REPORTS_DIR = "data/raw/FDIC_Call_Reports"
FAILURE_DATA_DIR = "data/raw/Failure_data"

# ==========================================================
# EDGAR Company Facts
# ==========================================================

banks = {
    "JPM": "0000019617",
    "BAC": "0000070858",
    "PNC": "0000713676",
    "SIVB": "0000719739",
}


def fetch_companyfacts(cik_num, credentials):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_num}.json"
    response = requests.get(url, headers=credentials)
    response.raise_for_status()
    return response.json()


# ==========================================================
# FDIC Call Reports
# ==========================================================

fdic_dict = {
    "JPM": "628",
    "BAC": "3510",
    "PNC": "6384",
    "SIVB": "24735",
    "FRCB": "59017",
    "SBNY": "57053",
}


def fetch_fdic_call_reports(cert_num, credentials):
    url = (
        "https://banks.data.fdic.gov/api/financials"
        f"?filters=CERT:{cert_num}"
        # DEPUNA = uninsured domestic deposits ($); DEPUNA + DEPFOR = DEP.
        "&fields=REPDTE,ASSET,DEP,DEPUNA,NETINC,ROA,ROE,EQ,NIMY,EEFFR"
        "&sort_by=REPDTE"
        "&sort_order=DESC"
        # 40 quarters (10 years) comfortably covers back through 2020 regardless
        # of how much time has passed since this was last run.
        "&limit=40"
    )

    response = requests.get(url, headers=credentials)
    response.raise_for_status()
    return response.json()


# ==========================================================
# FDIC Failure Data
# ==========================================================

failed_banks = {
    "SIVB": "24735",
    "FRCB": "59017",
    "SBNY": "57053",
}


def fetch_failure_data(cert_num, credentials):
    url = (
        "https://banks.data.fdic.gov/api/failures"
        f"?filters=CERT:{cert_num}"
        "&fields=NAME,CERT,FAILDATE,RESTYPE,COST,QBFDEP,QBFASSET"
    )

    response = requests.get(url, headers=credentials)
    response.raise_for_status()
    return response.json()


# ==========================================================
# Save Function
# ==========================================================

def save_api_data(items, credentials, output_dir, fetch_func):
    os.makedirs(output_dir, exist_ok=True)

    for name, identifier in items.items():
        filepath = os.path.join(output_dir, f"{name}.json")

        if os.path.exists(filepath):
            continue

        json_data = fetch_func(identifier, credentials)

        with open(filepath, "w") as outfile:
            json.dump(json_data, outfile)

        time.sleep(0.4)


# ==========================================================
# Main
# ==========================================================

save_api_data(
    banks,
    headers,
    COMPANYFACTS_DIR,
    fetch_companyfacts,
)

save_api_data(
    fdic_dict,
    headers,
    CALL_REPORTS_DIR,
    fetch_fdic_call_reports,
)

save_api_data(
    failed_banks,
    headers,
    FAILURE_DATA_DIR,
    fetch_failure_data,
)