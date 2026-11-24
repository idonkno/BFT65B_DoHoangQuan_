"""Part 1 - Acquisition & storage: resumable FDIC downloader, completeness checks,
Hive-partitioned Parquet conversion and storage benchmark.

Usage:
    python src/part1_acquisition_storage.py --quick-test     # 4 quarters
    python src/part1_acquisition_storage.py                  # full 1984-03-31 .. 2026-06-30
    python src/part1_acquisition_storage.py --skip-download  # only convert + benchmark
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BFT65B-pipeline/1.0"})


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------
def build_schema(with_year: bool = False) -> pa.Schema:
    fields = [pa.field("CERT", pa.int32()), pa.field("REPDTE", pa.int32()),
              pa.field("STNAME", pa.dictionary(pa.int8(), pa.string()))]
    # Column order follows C.NUM_FIELDS; amounts are float64 (exact for megabanks), ratios float32.
    fields += [pa.field(c, pa.float32() if c in C.RATIO_FIELDS else pa.float64())
               for c in C.NUM_FIELDS]
    if with_year:
        fields.append(pa.field("YEAR", pa.int32()))
    return pa.schema(fields)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def get_json(endpoint: str, params: dict, max_retries: int = 6, pause: float = 1.0) -> dict:
    url = f"{C.API_BASE}/{endpoint}"
    for attempt in range(1, max_retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=180)
            if r.status_code == 200:
                time.sleep(pause)
                return r.json()
            print(f"    HTTP {r.status_code} (attempt {attempt}) - retrying")
        except requests.RequestException as exc:
            print(f"    {type(exc).__name__} (attempt {attempt}) - retrying")
        time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"Giving up on {url} with {params}")


def fetch_all(endpoint: str, fields: list[str], filters: str | None = None,
              sort_by: str = "CERT") -> tuple[pd.DataFrame, int]:
    rows: list[dict] = []
    offset, total = 0, 0
    while True:
        params = {"fields": ",".join(fields), "limit": C.PAGE_SIZE, "offset": offset,
                  "sort_by": sort_by, "sort_order": "ASC", "format": "json"}
        if filters:
            params["filters"] = filters
        js = get_json(endpoint, params)
        total = js["meta"]["total"]
        page = [rec["data"] for rec in js["data"]]
        rows.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return pd.DataFrame(rows).drop(columns=["ID"], errors="ignore"), total


# ----------------------------------------------------------------------------
# Quarters
# ----------------------------------------------------------------------------
def quarter_ends(start: date, end: date) -> list[str]:
    out = []
    for y in range(start.year, end.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(y, m, d)
            if start <= q <= end:
                out.append(q.strftime("%Y%m%d"))
    return out


# ----------------------------------------------------------------------------
# Download (resumable, raw CSV per quarter, manifest after every quarter)
# ----------------------------------------------------------------------------
def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def download_financials(raw_dir: Path, quarters: list[str]) -> dict:
    fin_dir = raw_dir / "financials"
    fin_dir.mkdir(parents=True, exist_ok=True)
    mpath = raw_dir / "manifest_financials.json"
    manifest = load_manifest(mpath)
    for i, q in enumerate(quarters, 1):
        target = fin_dir / f"REPDTE={q}.csv"
        if target.exists() and manifest.get(q, {}).get("complete"):
            print(f"[{i}/{len(quarters)}] {q}: already complete, skipped")
            continue
        t0 = time.time()
        df, total = fetch_all("financials", C.FIN_FIELDS, filters=f"REPDTE:{q}")
        if df.empty:
            print(f"[{i}/{len(quarters)}] {q}: no data")
            continue
        df = df.reindex(columns=C.FIN_FIELDS)
        tmp = target.with_suffix(".tmp")
        df.to_csv(tmp, index=False)
        tmp.replace(target)  # atomic: never leave a half-written file
        complete = len(df) == total and not df.duplicated(C.KEY_FIELDS).any()
        manifest[q] = {"rows": len(df), "api_total": total, "complete": bool(complete),
                       "downloaded_at": datetime.now().isoformat(timespec="seconds"),
                       "seconds": round(time.time() - t0, 1)}
        mpath.write_text(json.dumps(manifest, indent=1))
        print(f"[{i}/{len(quarters)}] {q}: {len(df):>6,} rows (API {total:,}) "
              f"{'OK' if complete else 'INCOMPLETE'} {time.time() - t0:5.1f}s")
    return manifest


def download_small_tables(raw_dir: Path) -> None:
    for name, fields, sort_by in (("failures", C.FAIL_FIELDS, "FAILDATE"),
                                  ("institutions", C.INST_FIELDS, "CERT")):
        target = raw_dir / f"{name}.csv"
        if target.exists():
            print(f"{name:<12}: already present, skipped")
            continue
        df, total = fetch_all(name, fields, sort_by=sort_by)
        df.to_csv(target, index=False)
        print(f"{name:<12}: {len(df):>6,} rows (API {total:,})")


def read_raw_csvs(raw_dir: Path, quarters: list[str]) -> pa.Table:
    """Read raw CSVs straight into the explicit target schema."""
    schema = build_schema()
    conv = pacsv.ConvertOptions(
        column_types={f.name: (pa.string() if pa.types.is_dictionary(f.type) else f.type)
                      for f in schema},
        null_values=["", "NA", "null"], strings_can_be_null=True)
    tables = []
    for q in quarters:
        t = pacsv.read_csv(raw_dir / "financials" / f"REPDTE={q}.csv", convert_options=conv)
        tables.append(t.select(schema.names))
    t = pa.concat_tables(tables)
    t = t.set_column(t.schema.get_field_index("STNAME"), "STNAME",
                     pc.dictionary_encode(t["STNAME"]).cast(schema.field("STNAME").type))
    return t


def verify_completeness(raw_dir: Path, quarters: list[str], table: pa.Table) -> dict:
    manifest = load_manifest(raw_dir / "manifest_financials.json")
    keys = table.select(C.KEY_FIELDS).to_pandas()
    n_dup = int(keys.duplicated().sum())
    missing = [q for q in quarters if q not in manifest]
    bad = [q for q in quarters if q in manifest and not manifest[q]["complete"]]
    api_total = sum(manifest[q]["api_total"] for q in quarters if q in manifest)
    res = {"quarters": len(quarters), "rows": table.num_rows, "api_total_rows": api_total,
           "distinct_cert": int(keys["CERT"].nunique()), "duplicate_cert_repdte": n_dup,
           "missing_quarters": missing, "incomplete_quarters": bad,
           "ok": n_dup == 0 and not missing and not bad and table.num_rows == api_total}
    C.LOGS.mkdir(parents=True, exist_ok=True)
    (C.LOGS / "part1_completeness.json").write_text(json.dumps(res, indent=1))
    print("\n=== Completeness ===")
    for k, v in res.items():
        print(f"{k:<24}: {v}")
    return res


# ----------------------------------------------------------------------------
# Parquet conversion
# ----------------------------------------------------------------------------
def with_year(table: pa.Table) -> pa.Table:
    year = pc.cast(pc.floor(pc.divide(table["REPDTE"], 10000)), pa.int32())
    return table.append_column("YEAR", year)


def write_partitioned(table: pa.Table, out: Path, codec: str) -> None:
    if out.exists():
        shutil.rmtree(out)
    t = with_year(table)
    part = ds.partitioning(pa.schema([("YEAR", pa.int32())]), flavor="hive")
    ds.write_dataset(t, out, format="parquet", partitioning=part,
                     file_options=ds.ParquetFileFormat().make_write_options(compression=codec),
                     existing_data_behavior="overwrite_or_ignore", max_rows_per_group=128_000)


def write_single(table: pa.Table, out: Path, codec: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out, compression=codec, row_group_size=128_000)


# ----------------------------------------------------------------------------
# Benchmark
# ----------------------------------------------------------------------------
def dir_mb(path: Path) -> float:
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files) / 1024 ** 2


def timeit(fn: Callable[[], int], repeats: int = 3) -> tuple[float, int]:
    times, n = [], 0
    for _ in range(repeats):
        t0 = time.perf_counter()
        n = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), n


def run_benchmark(raw_dir: Path, quarters: list[str], table: pa.Table, state: str) -> pd.DataFrame:
    C.BENCH_DIR.mkdir(parents=True, exist_ok=True)
    cols = C.BENCH_COLUMNS
    csv_files = [raw_dir / "financials" / f"REPDTE={q}.csv" for q in quarters]
    csv_mb = sum(p.stat().st_size for p in csv_files) / 1024 ** 2
    dtypes = {c: ("float32" if c in C.RATIO_FIELDS else "float64") for c in C.NUM_FIELDS} \
        | {"CERT": "int32", "REPDTE": "int32"}

    def csv_full() -> int:
        return sum(len(pd.read_csv(p, dtype=dtypes)) for p in csv_files)

    def csv_filt() -> int:
        n = 0
        for p in csv_files:
            d = pd.read_csv(p, usecols=cols, dtype={k: v for k, v in dtypes.items() if k in cols})
            n += int((d["STNAME"] == state).sum())
        return n

    records: list[dict] = []

    def add(fmt: str, codec: str, mb: float, full: Callable, filt: Callable) -> None:
        tf, nf = timeit(full)
        tq, nq = timeit(filt)
        records.append({"format": fmt, "compression": codec, "size_mb": round(mb, 3),
                        "compression_ratio_vs_csv": round(csv_mb / mb, 2),
                        "full_scan_s": round(tf, 4), "filtered_query_s": round(tq, 4),
                        "rows_full": nf, "rows_filtered": nq})
        print(f"  {fmt:<20}{codec:<8}{mb:>9.2f} MB  full {tf:.3f}s  filt {tq:.3f}s")

    add("raw_csv", "none", csv_mb, csv_full, csv_filt)

    for codec in ("snappy", "zstd", "gzip"):
        single = C.BENCH_DIR / f"single_{codec}.parquet"
        write_single(table, single, codec)
        add("parquet_single", codec, dir_mb(single),
            lambda s=single: pq.read_table(s).num_rows,
            lambda s=single: pq.read_table(s, columns=cols,
                                           filters=[("STNAME", "==", state)]).num_rows)

        part = C.BENCH_DIR / f"partitioned_{codec}"
        write_partitioned(table, part, codec)

        def pfull(p: Path = part) -> int:
            return ds.dataset(p, format="parquet", partitioning="hive").to_table().num_rows

        def pfilt(p: Path = part) -> int:
            d = ds.dataset(p, format="parquet", partitioning="hive")
            return d.to_table(columns=cols, filter=ds.field("STNAME") == state).num_rows

        add("parquet_hive_YEAR", codec, dir_mb(part), pfull, pfilt)

    df = pd.DataFrame(records)
    C.TABLES.mkdir(parents=True, exist_ok=True)
    df.to_csv(C.TABLES / "part1_storage_benchmark.csv", index=False)
    return df


# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick-test", action="store_true", help="4 quarters only (2023), isolated folders")
    ap.add_argument("--start", default=C.FULL_START)
    ap.add_argument("--end", default=C.FULL_END)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--final-codec", default="zstd", choices=["snappy", "zstd", "gzip"],
                    help="codec for the production partitioned dataset")
    args = ap.parse_args()

    if args.quick_test:
        args.start, args.end = "2023-03-31", "2023-12-31"
        raw_dir = C.DATA_RAW.parent / "fdic_quick"
        C.BENCH_DIR = C.DATA_PROCESSED / "benchmark_quick"
        parquet_dir = C.DATA_PROCESSED / "financials_parquet_quick"
    else:
        raw_dir, parquet_dir = C.DATA_RAW, C.PARQUET_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    quarters = quarter_ends(date.fromisoformat(args.start), date.fromisoformat(args.end))
    print(f"Quarters: {len(quarters)} ({quarters[0]} -> {quarters[-1]}) raw={raw_dir}\n")
    if not args.skip_download:
        download_financials(raw_dir, quarters)
        download_small_tables(raw_dir)

    table = read_raw_csvs(raw_dir, quarters)
    res = verify_completeness(raw_dir, quarters, table)
    if not res["ok"]:
        print("Completeness check FAILED - aborting before conversion")
        sys.exit(1)

    print(f"\nWriting production Hive dataset ({args.final_codec}) -> {parquet_dir}")
    write_partitioned(table, parquet_dir, args.final_codec)

    state = C.BENCH_STATE
    states = table["STNAME"].to_pandas()
    if state not in set(states.astype(str)):
        state = states.astype(str).value_counts().index[0]
    print(f"\nBenchmark (state={state}, columns={C.BENCH_COLUMNS})")
    bench = run_benchmark(raw_dir, quarters, table, state)

    print("\n=== Summary ===")
    print(bench.drop(columns=["rows_full"]).to_string(index=False))
    print(f"\nrows={res['rows']:,}  quarters={res['quarters']}  distinct_cert={res['distinct_cert']:,}  "
          f"dups={res['duplicate_cert_repdte']}")
    print("Files in outputs/:")
    for p in sorted(C.OUTPUTS.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(C.ROOT)}  ({p.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
