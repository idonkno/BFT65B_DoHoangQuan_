"""Part 3 - Scalable processing: identical CAMELS workload on Pandas, Polars (lazy) and PySpark.

Workload (identical semantics in every engine)
  * load clean_panel.parquet with column pruning
  * ratios (denominator must be > 0, else null): CAP=EQ/ASSET, NPL=NCLNLS/LNLSGR, LTD=LNLSNET/DEP,
    LIQ=(CHBAL+SC)/ASSET, EFF=NONIX/(INTINC-EINTEXP+NONII); ROA/ROE passed through (float64)
  * per-bank windows over (CERT, REPDTE), gap-safe via quarter index QIDX:
    ASSET_G4 / LOAN_G4 (vs 4 quarters earlier), ROA_MEAN4 / ROA_STD4 (4 consecutive quarters, ddof=1),
    NPL_QOQ (vs previous quarter)
  * peer stats by (REPDTE, STNAME): PEER_N, mean + z-score (std ddof=1, std>1e-9) of CAP, NPL, ROA

Each timed run happens in a FRESH subprocess (clean RAM); RAM is sampled with psutil (process + children,
so the Spark JVM is included). Time = median of 3 runs of load+compute+materialise.

Usage: python src/part3_scalable_processing.py [--runs 3]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import polars as pl
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

ENGINES = ["pandas", "polars", "pyspark"]
NUM_IN = ["ASSET", "EQ", "NCLNLS", "LNLSGR", "ROA", "ROE", "LNLSNET", "DEP", "CHBAL", "SC",
          "NONIX", "INTINC", "EINTEXP", "NONII"]
COLS = ["CERT", "REPDTE", "STNAME"] + NUM_IN
PEER_METRICS = ["CAP", "NPL", "ROA"]
STD_EPS = 1e-9
OUT_COLS = (["CERT", "REPDTE", "CAP", "NPL", "ROA", "ROE", "LTD", "LIQ", "EFF",
             "ASSET_G4", "LOAN_G4", "ROA_MEAN4", "ROA_STD4", "NPL_QOQ", "PEER_N"]
            + [f"{m}_PEER_MEAN" for m in PEER_METRICS] + [f"{m}_Z" for m in PEER_METRICS])
COMPARE_COLS = [c for c in OUT_COLS if c not in ("CERT", "REPDTE")]
PANEL = C.CLEAN_PANEL
PART3_DIR = C.DATA_PROCESSED / "part3"


# ----------------------------------------------------------------------------
# Engine 1: pandas
# ----------------------------------------------------------------------------
def run_pandas(path: Path = PANEL) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=COLS)
    df = df.astype({c: "float64" for c in NUM_IN} | {"CERT": "int64", "REPDTE": "int64"})
    df = df.sort_values(["CERT", "REPDTE"], kind="stable").reset_index(drop=True)

    def sdiv(n: pd.Series, d: pd.Series) -> pd.Series:
        return n / d.where(d > 0)

    df["CAP"] = sdiv(df["EQ"], df["ASSET"])
    df["NPL"] = sdiv(df["NCLNLS"], df["LNLSGR"])
    df["LTD"] = sdiv(df["LNLSNET"], df["DEP"])
    df["LIQ"] = sdiv(df["CHBAL"] + df["SC"], df["ASSET"])
    df["EFF"] = sdiv(df["NONIX"], df["INTINC"] - df["EINTEXP"] + df["NONII"])
    df["QIDX"] = (df["REPDTE"] // 10000) * 4 + ((df["REPDTE"] // 100) % 100 - 1) // 3

    g = df.groupby("CERT", sort=False)
    q = df["QIDX"]

    def lag(col: str, k: int) -> pd.Series:
        return g[col].shift(k)

    ok = {k: (q - lag("QIDX", k)) == k for k in (1, 3, 4)}
    la, ll = lag("ASSET", 4), lag("LNLSGR", 4)
    df["ASSET_G4"] = (df["ASSET"] / la.where(la > 0) - 1).where(ok[4])
    df["LOAN_G4"] = (df["LNLSGR"] / ll.where(ll > 0) - 1).where(ok[4])
    arr = np.column_stack([lag("ROA", k).to_numpy() for k in range(4)])  # NaN if any lag missing
    with np.errstate(invalid="ignore"):
        df["ROA_MEAN4"] = pd.Series(arr.mean(axis=1)).where(ok[3])
        df["ROA_STD4"] = pd.Series(arr.std(axis=1, ddof=1)).where(ok[3])
    df["NPL_QOQ"] = (df["NPL"] - lag("NPL", 1)).where(ok[1])

    gp = df.groupby(["REPDTE", "STNAME"], dropna=False, sort=False)
    df["PEER_N"] = gp["CAP"].transform("size").astype("float64")
    for m in PEER_METRICS:
        mean, std = gp[m].transform("mean"), gp[m].transform("std")
        df[f"{m}_PEER_MEAN"] = mean
        df[f"{m}_Z"] = (df[m] - mean) / std.where(std > STD_EPS)
    return df[OUT_COLS]


# ----------------------------------------------------------------------------
# Engine 2: polars lazy
# ----------------------------------------------------------------------------
def polars_lazy(path: Path = PANEL) -> pl.LazyFrame:
    lf = (pl.scan_parquet(path).select(COLS)
          .with_columns([pl.col(c).cast(pl.Float64) for c in NUM_IN]
                        + [pl.col("CERT").cast(pl.Int64), pl.col("REPDTE").cast(pl.Int64)])
          .sort(["CERT", "REPDTE"]))

    def sdiv(n: pl.Expr, d: pl.Expr) -> pl.Expr:
        return pl.when(d > 0).then(n / d)

    c = pl.col
    lf = lf.with_columns(
        CAP=sdiv(c("EQ"), c("ASSET")), NPL=sdiv(c("NCLNLS"), c("LNLSGR")),
        LTD=sdiv(c("LNLSNET"), c("DEP")), LIQ=sdiv(c("CHBAL") + c("SC"), c("ASSET")),
        EFF=sdiv(c("NONIX"), c("INTINC") - c("EINTEXP") + c("NONII")),
        QIDX=(c("REPDTE") // 10000) * 4 + ((c("REPDTE") // 100) % 100 - 1) // 3,
    )
    q = c("QIDX")
    ok = {k: (q - q.shift(k).over("CERT")) == k for k in (1, 3, 4)}
    la, ll = c("ASSET").shift(4).over("CERT"), c("LNLSGR").shift(4).over("CERT")
    lf = lf.with_columns(
        ASSET_G4=pl.when(ok[4] & (la > 0)).then(c("ASSET") / la - 1),
        LOAN_G4=pl.when(ok[4] & (ll > 0)).then(c("LNLSGR") / ll - 1),
        ROA_MEAN4=pl.when(ok[3]).then(c("ROA").rolling_mean(window_size=4, min_periods=4).over("CERT")),
        ROA_STD4=pl.when(ok[3]).then(c("ROA").rolling_std(window_size=4, min_periods=4).over("CERT")),
        NPL_QOQ=pl.when(ok[1]).then(c("NPL") - c("NPL").shift(1).over("CERT")),
        PEER_N=pl.len().over(["REPDTE", "STNAME"]).cast(pl.Float64),
    )
    keys = ["REPDTE", "STNAME"]
    lf = lf.with_columns([c(m).mean().over(keys).alias(f"{m}_PEER_MEAN") for m in PEER_METRICS]
                         + [c(m).std().over(keys).alias(f"{m}_STD") for m in PEER_METRICS])
    lf = lf.with_columns([pl.when(c(f"{m}_STD") > STD_EPS)
                          .then((c(m) - c(f"{m}_PEER_MEAN")) / c(f"{m}_STD")).alias(f"{m}_Z")
                          for m in PEER_METRICS])
    return lf.select(OUT_COLS)


def run_polars(path: Path = PANEL) -> pl.DataFrame:
    return polars_lazy(path).collect()


# ----------------------------------------------------------------------------
# Engine 3: PySpark (local mode)
# ----------------------------------------------------------------------------
def ensure_java() -> None:
    if shutil.which("java"):
        return
    root = C.ROOT / "tools" / "jdk"
    for exe in root.rglob("java.exe") if root.exists() else []:
        home = exe.parent.parent
        os.environ["JAVA_HOME"] = str(home)
        os.environ["PATH"] = str(home / "bin") + os.pathsep + os.environ["PATH"]
        return
    raise RuntimeError("Java not found: PySpark needs a JDK (8/11/17). Set JAVA_HOME.")


def make_spark():  # type: ignore[no-untyped-def]
    ensure_java()
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable
    from pyspark.sql import SparkSession
    cores = os.cpu_count() or 4
    return (SparkSession.builder.master("local[*]").appName("bft65b-part3")
            .config("spark.sql.shuffle.partitions", str(min(2 * cores, 64)))
            .config("spark.driver.memory", "6g")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.execution.arrow.pyspark.enabled", "true")
            .config("spark.sql.session.timeZone", "UTC")
            .getOrCreate())


def spark_df(spark, path: Path = PANEL):  # type: ignore[no-untyped-def]
    from pyspark.sql import Window, functions as F
    df = spark.read.parquet(str(path).replace("\\", "/")).select(*COLS)
    df = df.select([F.col(c).cast("double").alias(c) for c in NUM_IN]
                   + [F.col("CERT").cast("long"), F.col("REPDTE").cast("long"), "STNAME"])

    def sdiv(n, d):  # type: ignore[no-untyped-def]
        return F.when(d > 0, n / d)

    c = F.col
    df = df.withColumns({
        "CAP": sdiv(c("EQ"), c("ASSET")), "NPL": sdiv(c("NCLNLS"), c("LNLSGR")),
        "LTD": sdiv(c("LNLSNET"), c("DEP")), "LIQ": sdiv(c("CHBAL") + c("SC"), c("ASSET")),
        "EFF": sdiv(c("NONIX"), c("INTINC") - c("EINTEXP") + c("NONII")),
        "QIDX": (F.floor(c("REPDTE") / 10000) * 4
                 + F.floor((F.floor(c("REPDTE") / 100) % 100 - 1) / 3)).cast("long"),
    })
    w = Window.partitionBy("CERT").orderBy("REPDTE")
    w4 = w.rowsBetween(-3, 0)
    ok = {k: (c("QIDX") - F.lag("QIDX", k).over(w)) == k for k in (1, 3, 4)}
    la, ll = F.lag("ASSET", 4).over(w), F.lag("LNLSGR", 4).over(w)
    full4 = F.count("ROA").over(w4) == 4
    wp = Window.partitionBy("REPDTE", "STNAME")
    df = df.withColumns({
        "ASSET_G4": F.when(ok[4] & (la > 0), c("ASSET") / la - 1),
        "LOAN_G4": F.when(ok[4] & (ll > 0), c("LNLSGR") / ll - 1),
        "ROA_MEAN4": F.when(ok[3] & full4, F.avg("ROA").over(w4)),
        "ROA_STD4": F.when(ok[3] & full4, F.stddev_samp("ROA").over(w4)),
        "NPL_QOQ": F.when(ok[1], c("NPL") - F.lag("NPL", 1).over(w)),
        "PEER_N": F.count(F.lit(1)).over(wp).cast("double"),
        **{f"{m}_PEER_MEAN": F.avg(m).over(wp) for m in PEER_METRICS},
        **{f"{m}_STD": F.stddev_samp(m).over(wp) for m in PEER_METRICS},
    })
    df = df.withColumns({f"{m}_Z": F.when(c(f"{m}_STD") > STD_EPS,
                                          (c(m) - c(f"{m}_PEER_MEAN")) / c(f"{m}_STD"))
                         for m in PEER_METRICS})
    return df.select(*OUT_COLS)


def run_spark(spark) -> None:  # type: ignore[no-untyped-def]
    spark_df(spark).write.format("noop").mode("overwrite").save()


# ----------------------------------------------------------------------------
# Memory monitor + worker
# ----------------------------------------------------------------------------
class MemMonitor:
    """Samples RSS of this process + children (Spark JVM) in a background thread."""

    def __init__(self, interval: float = 0.02) -> None:
        self.proc, self.interval = psutil.Process(), interval
        self.peak, self._stop = 0, threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def total(self) -> int:
        rss = self.proc.memory_info().rss
        for ch in self.proc.children(recursive=True):
            try:
                rss += ch.memory_info().rss
            except psutil.Error:
                pass
        return rss

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self.total())
            time.sleep(self.interval)

    def start(self) -> None:
        self.peak = self.total()
        self._t.start()

    def stop(self) -> int:
        self._stop.set()
        self._t.join()
        self.peak = max(self.peak, self.total())
        return self.peak


def worker(engine: str) -> None:
    mb = 1024 ** 2
    spark, startup = None, 0.0
    if engine == "pyspark":
        t = time.perf_counter()
        spark = make_spark()
        spark.range(10).count()  # force JVM init before timing
        startup = time.perf_counter() - t
    import gc
    gc.collect()
    mon = MemMonitor()
    base = mon.total()
    mon.start()
    t0 = time.perf_counter()
    if engine == "pandas":
        res = run_pandas()
        rows = len(res)
    elif engine == "polars":
        res = run_polars()
        rows = res.height
    else:
        run_spark(spark)
        rows = -1
    wall = time.perf_counter() - t0
    peak = mon.stop()
    print("RESULT " + json.dumps({"engine": engine, "wall_s": wall, "peak_mb": peak / mb,
                                  "baseline_mb": base / mb, "startup_s": startup, "rows": rows}))
    if spark is not None:
        spark.stop()


# ----------------------------------------------------------------------------
# Equivalence check
# ----------------------------------------------------------------------------
def to_np(df: pd.DataFrame | pl.DataFrame, col: str) -> np.ndarray:
    if isinstance(df, pl.DataFrame):
        return df[col].cast(pl.Float64).fill_null(np.nan).to_numpy()
    return df[col].astype("float64").to_numpy()


def compare(a: pd.DataFrame | pl.DataFrame, b: pd.DataFrame | pl.DataFrame, pair: str) -> list[dict]:
    rows = []
    for col in COMPARE_COLS:
        x, y = to_np(a, col), to_np(b, col)
        nx, ny = np.isnan(x), np.isnan(y)
        both = ~nx & ~ny
        diff = np.abs(x[both] - y[both])
        rows.append({"pair": pair, "column": col, "rows": len(x), "null_mismatch": int((nx != ny).sum()),
                     "both_non_null": int(both.sum()),
                     "max_abs_diff": float(diff.max()) if diff.size else 0.0,
                     "mean_abs_diff": float(diff.mean()) if diff.size else 0.0})
    for r in rows:
        r["passed"] = bool(r["null_mismatch"] == 0 and r["max_abs_diff"] < 1e-5)
    return rows


def verify() -> None:
    pdf = pl.from_pandas(run_pandas()).sort(["CERT", "REPDTE"])
    plf = run_polars().sort(["CERT", "REPDTE"])
    spark = make_spark()
    spdf = spark_df(spark).toPandas()
    spl = pl.from_pandas(spdf).sort(["CERT", "REPDTE"])
    assert plf.height == spl.height == pdf.height, "row count mismatch"
    for k in ("CERT", "REPDTE"):
        assert (plf[k] == spl[k]).all() and (plf[k] == pdf[k]).all(), f"key mismatch on {k}"
    rows = compare(plf, spl, "polars_vs_pyspark") + compare(pdf, plf, "pandas_vs_polars")
    out = pd.DataFrame(rows)
    out.to_csv(C.TABLES / "part3_equivalence_check.csv", index=False)
    print(out.to_string(index=False, float_format=lambda v: f"{v:.3e}"))
    assert out["passed"].all(), "EQUIVALENCE FAILED"
    assert out["max_abs_diff"].max() < 1e-5
    print(f"\nEQUIVALENCE OK: {len(out)} checks, max_abs_diff={out['max_abs_diff'].max():.3e}, "
          f"null_mismatch={int(out['null_mismatch'].sum())}")
    spark.stop()


# ----------------------------------------------------------------------------
# Plans, benchmark, report
# ----------------------------------------------------------------------------
def save_plans() -> None:
    lf = polars_lazy()
    (C.REPORTS / "polars_query_plan.txt").write_text(
        "=== UNOPTIMIZED PLAN ===\n" + lf.explain(optimized=False)
        + "\n\n=== OPTIMIZED PLAN ===\n" + lf.explain(optimized=True), encoding="utf-8")
    spark = make_spark()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        spark_df(spark).explain(mode="extended")
    (C.REPORTS / "spark_execution_plan.txt").write_text(buf.getvalue(), encoding="utf-8")
    spark.stop()


def run_worker_subprocess(engine: str) -> dict:
    p = subprocess.run([sys.executable, str(Path(__file__)), "--worker", engine],
                       capture_output=True, text=True, encoding="utf-8")
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    if p.returncode != 0 or not lines:
        raise RuntimeError(f"{engine} worker failed:\n{p.stderr[-3000:]}\n{p.stdout[-1000:]}")
    return json.loads(lines[-1][7:])


def benchmark(runs: int) -> pd.DataFrame:
    recs = []
    for eng in ENGINES:
        res = []
        for i in range(runs):
            r = run_worker_subprocess(eng)
            res.append(r)
            print(f"  {eng:<8} run {i + 1}: {r['wall_s']:7.2f}s  peak {r['peak_mb']:8.0f} MB")
        recs.append({"engine": eng, "wall_time_s": statistics.median(r["wall_s"] for r in res),
                     "peak_ram_mb": statistics.median(r["peak_mb"] for r in res),
                     "baseline_ram_mb": statistics.median(r["baseline_mb"] for r in res),
                     "spark_startup_s": statistics.median(r["startup_s"] for r in res),
                     "all_runs_s": ";".join(f"{r['wall_s']:.2f}" for r in res)})
    df = pd.DataFrame(recs)
    base = df.loc[df["engine"] == "pandas", "wall_time_s"].iloc[0]
    df["speedup_vs_pandas"] = base / df["wall_time_s"]
    df["marginal_ram_mb"] = df["peak_ram_mb"] - df["baseline_ram_mb"]
    df = df.round(3)
    df.to_csv(C.TABLES / "part3_engine_benchmark.csv", index=False)
    return df


def fig_benchmark(df: pd.DataFrame) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#8c8c8c", "#1f77b4", "#e8710a"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for ax, col, title, unit in ((axes[0], "wall_time_s", "Wall time (median of 3)", "seconds"),
                                 (axes[1], "peak_ram_mb", "Peak RAM (process tree)", "MB")):
        bars = ax.bar(df["engine"], df[col], color=colors)
        ax.bar_label(bars, fmt="%.1f", padding=2)
        ax.set_title(title)
        ax.set_ylabel(unit)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle(f"Engine benchmark - CAMELS workload, {PANEL.name}", fontweight="bold")
    fig.tight_layout()
    fig.savefig(C.FIGURES / "fig3_engine_benchmark.png", dpi=140)
    plt.close(fig)


def cluster_analysis(df: pd.DataFrame, rows: int) -> str:
    total_mb = psutil.virtual_memory().total / 1024 ** 2
    usable = 0.8 * total_mb
    lines = [f"CLUSTER THRESHOLD ANALYSIS (linear extrapolation of measured marginal RAM)",
             f"Base workload: {rows:,} rows, panel parquet {PANEL.stat().st_size / 1024 ** 2:.0f} MB; "
             f"laptop RAM {total_mb / 1024:.1f} GB, usable (80%) {usable / 1024:.1f} GB, {os.cpu_count()} cores.",
             "Assumptions: peak = baseline + marginal * scale; time = wall * scale; Spark local heap is capped "
             "at spark.driver.memory=6g, so beyond that it spills to disk (time grows super-linearly) "
             "- its RSS estimate is a lower bound.", ""]
    hdr = f"{'engine':<8}{'scale':>7}{'rows (M)':>10}{'est peak GB':>13}{'est time s':>12}  fits in 80% RAM?"
    lines.append(hdr)
    maxscale: dict[str, float] = {}
    for _, r in df.iterrows():
        maxscale[r["engine"]] = (usable - r["baseline_ram_mb"]) / max(r["marginal_ram_mb"], 1e-9)
        for s in (1, 10, 100):
            peak = r["baseline_ram_mb"] + r["marginal_ram_mb"] * s
            lines.append(f"{r['engine']:<8}{s:>6}x{rows * s / 1e6:>10.1f}{peak / 1024:>13.1f}"
                         f"{r['wall_time_s'] * s:>12.0f}  {'YES' if peak <= usable else 'NO'}")
    lines += ["", "Max in-memory scale factor on this laptop (80% RAM):"]
    lines += [f"  {e:<8}: {s:6.1f}x  (~{rows * s / 1e6:,.0f} M rows)" for e, s in maxscale.items()]
    node_gb = 64 * 0.6
    for e in ("pandas", "polars"):
        r = df[df["engine"] == e].iloc[0]
        need = r["marginal_ram_mb"] * 100 / 1024
        lines.append(f"At 100x, {e} needs ~{need:.0f} GB working set -> >= {math.ceil(need / node_gb)} "
                     f"executors of 64 GB (60% usable) if distributed.")
    best = min(maxscale["pandas"], maxscale["polars"])
    lines += ["", f"DECISION RULE: single-machine Pandas/Polars is sufficient up to ~{best:.0f}x "
                  f"(~{rows * best / 1e6:,.0f} M rows). Beyond that (or when the working set > 80% RAM, "
                  "or a job must finish within an SLA that 1 node cannot meet), a Spark cluster is justified.",
              "At 10x (16.8M rows) the workload still fits a laptop; at 100x (168M rows) it does not."]
    text = "\n".join(lines)
    (C.REPORTS / "cluster_threshold_analysis.txt").write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", choices=ENGINES)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    if a.worker:
        worker(a.worker)
        return
    for d in (C.TABLES, C.REPORTS, C.FIGURES):
        d.mkdir(parents=True, exist_ok=True)
    if a.verify:
        verify()
        return
    print("== Query plans ==")
    save_plans()
    print("== Equivalence check ==")
    p = subprocess.run([sys.executable, str(Path(__file__)), "--verify"], capture_output=True,
                       text=True, encoding="utf-8")
    print(p.stdout)
    if p.returncode != 0:
        print(p.stderr[-3000:])
        sys.exit("Equivalence check failed")
    print("== Benchmark ==")
    df = benchmark(a.runs)
    fig_benchmark(df)
    rows = pl.scan_parquet(PANEL).select(pl.len()).collect().item()
    print(cluster_analysis(df, rows))
    print("\n=== Benchmark summary ===")
    print(df.drop(columns=["all_runs_s"]).to_string(index=False))
    print("\nFiles in outputs/ (part3):")
    for p_ in sorted(C.OUTPUTS.rglob("*")):
        if p_.is_file() and ("part3" in p_.name or p_.name in (
                "fig3_engine_benchmark.png", "polars_query_plan.txt", "spark_execution_plan.txt",
                "cluster_threshold_analysis.txt")):
            print(f"  {p_.relative_to(C.ROOT)}  ({p_.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
