import json
import pandas as pd
import pyarrow
import fastparquet
from pathlib import Path

folder = Path("data/processed")

def load_json(filepath):
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)


    cik = data.get("cik")
    company = data.get("company_name")
    records = []

    for year, metric in data.get("annual_metrics").items():
        for metric_name, metric_data in metric.items():
            if not isinstance(metric_data, dict):
                continue
            records.append({
                    "cik": cik,
                    "company_name": company,
                    "source_file": data.get("source_file"),
                    "year": int(year),
                    "quarter": "FY",
                    "metric": metric_name,
                    "value_usd": metric_data.get("value_usd"),
                    "concept": metric_data.get("concept"),
                    "end_date": metric_data.get("end_date"),
                    "filed": metric_data.get("filed"),
                    "accession_number": metric_data.get("accession_number"),
                })
    return records


def load_quarterly_json(filepath):
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    cik = data.get("cik")
    company = data.get("company_name")
    year = int(filepath.stem.split("_")[1])
    records = []

    for quarter, metric in data.get("quarterly_metrics").items():
        for metric_name, metric_data in metric.items():
            if not isinstance(metric_data, dict):
                continue
            records.append({
                    "cik": cik,
                    "company_name": company,
                    "source_file": data.get("source_file"),
                    "year": year,
                    "quarter": quarter,
                    "metric": metric_name,
                    "value_usd": metric_data.get("value_usd"),
                    "concept": metric_data.get("concept"),
                    "end_date": metric_data.get("end_date"),
                    "filed": metric_data.get("filed"),
                    "accession_number": metric_data.get("accession_number"),
                })
    return records


def main():
    all_records = []

    for json_file in folder.glob("*.json"):
        all_records.extend(load_json(json_file))

    for json_file in folder.glob("10-Q/*/*.json"):
        all_records.extend(load_quarterly_json(json_file))

    df = pd.DataFrame(all_records)
    df["source_file"] = df["source_file"].apply(lambda x: str(x))
    df["value_usd"] = df["value_usd"].astype("int64")
    df.to_parquet("data/processed/processed_dataframes/DataframeLong.parquet")


if __name__ == "__main__":
    main()

