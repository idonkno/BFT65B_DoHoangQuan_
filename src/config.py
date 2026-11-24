"""Centralized configuration: paths, seed, date boundaries, FDIC field lists."""
from __future__ import annotations

from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
DATA_RAW: Path = ROOT / "data_raw" / "fdic"
DATA_PROCESSED: Path = ROOT / "data_processed"
PARQUET_DIR: Path = DATA_PROCESSED / "financials_parquet"  # Hive-partitioned by YEAR
BENCH_DIR: Path = DATA_PROCESSED / "benchmark"
OUTPUTS: Path = ROOT / "outputs"
TABLES: Path = OUTPUTS / "tables"
FIGURES: Path = OUTPUTS / "figures"
LOGS: Path = OUTPUTS / "logs"

SEED: int = 42

FULL_START = "1984-03-31"
FULL_END = "2026-06-30"
DEV_END = "2006-12-31"
TEST_START = "2008-03-31"
MODEL_END = "2025-06-30"

API_BASE = "https://api.fdic.gov/banks"
PAGE_SIZE = 10_000

KEY_FIELDS = ["CERT", "REPDTE"]
CAT_FIELDS = ["STNAME"]
NUM_FIELDS = [
    "ASSET", "DEP", "DEPDOM", "EQ", "BRO", "CHBAL", "SC",
    "LNLSGR", "LNLSNET", "LNRE", "LNCI", "LNCON", "LNATRES",
    "NCLNLS", "P3ASSET", "P9ASSET", "NAASSET", "NTLNLS", "ORE",
    "NETINC", "INTINC", "EINTEXP", "NONII", "NONIX", "ELNATR",
    "ROA", "ROE", "NIMY",
    "RBC1AAJ", "RBCRWAJ",
]
RATIO_FIELDS = ["ROA", "ROE", "NIMY", "RBC1AAJ", "RBCRWAJ"]  # in %
AMOUNT_FIELDS = [c for c in NUM_FIELDS if c not in RATIO_FIELDS]  # thousands of USD
FIN_FIELDS = KEY_FIELDS + CAT_FIELDS + NUM_FIELDS

FAIL_FIELDS = ["CERT", "NAME", "CITYST", "FAILDATE", "FAILYR", "COST", "QBFASSET", "QBFDEP", "RESTYPE"]
INST_FIELDS = ["CERT", "NAME", "STNAME", "BKCLASS", "ACTIVE", "ESTYMD", "ENDEFYMD", "CHANGEC1", "INSDATE"]

# Benchmark query: one state + 5 columns
BENCH_STATE = "CALIFORNIA"
BENCH_COLUMNS = ["CERT", "REPDTE", "STNAME", "ASSET", "EQ"]

REPORTS: Path = OUTPUTS / 'reports'
CLEAN_PANEL: Path = DATA_PROCESSED / 'clean_panel.parquet'
FAILURES_CSV: Path = DATA_RAW / 'failures.csv'
INSTITUTIONS_CSV: Path = DATA_RAW / 'institutions.csv'

LABEL_HORIZON = '1y'  # 4 quarters
EQ_RATIO_EXTREME = 0.5  # |EQ/ASSET| above this is flagged

BASEL_START = '1990-01-01'  # risk-based capital ratios reported from 1990
BASEL_FIELDS = ['RBC1AAJ', 'RBCRWAJ']
