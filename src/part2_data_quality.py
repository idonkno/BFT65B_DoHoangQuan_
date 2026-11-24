"""Part 2 - Data quality audit, deterministic cleaning, point-in-time panel (Polars).

Leakage rules enforced here
---------------------------
* Cleaning rules are ROW-LOCAL (use only the row's own report-date values) - no
  statistic, imputation or drop decision uses data from other/future quarters.
* Structural NaNs are preserved (imputation belongs inside the CV pipeline).
* Target y = 1 iff REPDTE < FAILDATE <= REPDTE + 4 quarters; mergers/voluntary exits = 0.
* Columns derived from failure/exit info are prefixed META_ / LABEL_ and MUST NOT be features.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402
import seaborn as sns  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
pl.enable_string_cache()
pl.Config.set_tbl_rows(80)
pl.Config.set_tbl_cols(12)
pl.Config.set_fmt_str_lengths(60)

Rec = dict[str, Any]


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def load_financials() -> pl.DataFrame:
    df = pl.read_parquet(C.PARQUET_DIR / "**" / "*.parquet", hive_partitioning=True)
    df = df.with_columns(pl.col("STNAME").cast(pl.Utf8))
    df = df.with_columns(pl.col(pl.Float32, pl.Float64).fill_nan(None))
    return df.with_columns(
        REPDATE=pl.col("REPDTE").cast(pl.Utf8).str.to_date("%Y%m%d"),
        YEAR=pl.col("YEAR").cast(pl.Int32),
    )


def mask_disguised_missing(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Pre-Basel (REPDATE < C.BASEL_START) risk-based capital ratios are stored as 0, not null.
    Calendar-based, row-local rule: set them to null (structural NaN). Returns (df, cells changed)."""
    pre = pl.col("REPDATE") < pl.lit(C.BASEL_START).str.to_date()
    n = int(sum(df.filter(pre & (pl.col(c) == 0)).height for c in C.BASEL_FIELDS))
    out = df.with_columns([pl.when(pre & (pl.col(c) == 0)).then(None).otherwise(pl.col(c)).alias(c)
                           for c in C.BASEL_FIELDS])
    return out, n


def _to_cert(col: str = "CERT") -> pl.Expr:
    return pl.col(col).cast(pl.Float64, strict=False).cast(pl.Int32, strict=False).alias("CERT")


def _date(col: str) -> pl.Expr:
    return pl.col(col).str.to_date("%m/%d/%Y", strict=False).alias(col)


def load_failures() -> pl.DataFrame:
    df = pl.read_csv(C.FAILURES_CSV, infer_schema_length=0)
    return df.with_columns(
        _to_cert(), _date("FAILDATE"),
        pl.col("COST").cast(pl.Float64, strict=False),
        pl.col("QBFASSET").cast(pl.Float64, strict=False),
        pl.col("QBFDEP").cast(pl.Float64, strict=False),
        pl.col("FAILYR").cast(pl.Int32, strict=False),
    )


def load_institutions() -> pl.DataFrame:
    df = pl.read_csv(C.INSTITUTIONS_CSV, infer_schema_length=0)
    return df.with_columns(
        _to_cert(), _date("ESTYMD"), _date("ENDEFYMD"), _date("INSDATE"),
        pl.col("ACTIVE").cast(pl.Int8, strict=False),
        pl.col("CHANGEC1").cast(pl.Float64, strict=False).cast(pl.Int32, strict=False),
    )


# ----------------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------------
def decade_expr() -> pl.Expr:
    return ((pl.col("YEAR") // 10) * 10).cast(pl.Utf8).add("s").alias("ERA")


def rec(table: str, check: str, group: str, field: str, n_total: int, n_flag: int) -> Rec:
    return {"table": table, "check": check, "group": group, "field": field, "n_total": int(n_total),
            "n_flagged": int(n_flag), "rate": round(n_flag / n_total, 6) if n_total else None}


def missingness_records(df: pl.DataFrame, table: str, cols: list[str], era: pl.Expr | None) -> list[Rec]:
    out: list[Rec] = []
    n = df.height
    nulls = df.select([pl.col(c).is_null().sum().alias(c) for c in cols]).row(0, named=True)
    out += [rec(table, "missing", "ALL", c, n, nulls[c]) for c in cols]
    if era is not None:
        g = (df.with_columns(era).group_by("ERA", maintain_order=False)
             .agg([pl.len().alias("_n")] + [pl.col(c).is_null().sum().alias(c) for c in cols]).sort("ERA"))
        for r in g.iter_rows(named=True):
            out += [rec(table, "missing", r["ERA"], c, r["_n"], r[c]) for c in cols]
    return out


def missingness_by_year(df: pl.DataFrame) -> pl.DataFrame:
    cols = C.FIN_FIELDS[2:]  # STNAME + numeric fields
    return (df.group_by("YEAR").agg([pl.len().alias("n_rows")] +
                                    [pl.col(c).is_null().mean().alias(c) for c in cols]).sort("YEAR"))


def structural_shifts(by_year: pl.DataFrame) -> dict[str, Any]:
    """Per field: first/last year with missing rate < 50% and overall range of yearly rates."""
    out: dict[str, Any] = {}
    years = by_year["YEAR"].to_list()
    for c in by_year.columns[2:]:
        rates = by_year[c].to_list()
        ok = [y for y, r in zip(years, rates) if r < 0.5]
        out[c] = {"first_year_available": ok[0] if ok else None,
                  "min_yearly_missing": round(min(rates), 4), "max_yearly_missing": round(max(rates), 4),
                  "structural_shift": bool(max(rates) - min(rates) > 0.5)}
    return out


def anomaly_exprs() -> dict[str, pl.Expr]:
    ratio = pl.col("EQ") / pl.col("ASSET")
    pos = pl.col("ASSET") > 0
    return {
        "ASSET_le_0": pl.col("ASSET") <= 0,
        "ASSET_null": pl.col("ASSET").is_null(),
        "DEP_lt_0": pl.col("DEP") < 0,
        "LNLSNET_gt_LNLSGR": pl.col("LNLSNET") > pl.col("LNLSGR"),
        "LNATRES_lt_0": pl.col("LNATRES") < 0,
        "EQ_lt_0": pl.col("EQ") < 0,
        "EQ_ASSET_extreme": pos & (ratio.abs() > C.EQ_RATIO_EXTREME),
        "EQ_gt_ASSET": pos & (pl.col("EQ") > pl.col("ASSET")),
        "DEP_gt_ASSET": pos & (pl.col("DEP") > pl.col("ASSET")),
    }


def anomaly_records(df: pl.DataFrame) -> list[Rec]:
    ex = anomaly_exprs()
    out: list[Rec] = []
    tot = df.select([e.fill_null(False).sum().alias(k) for k, e in ex.items()]).row(0, named=True)
    out += [rec("financials", "anomaly", "ALL", k, df.height, v) for k, v in tot.items()]
    g = (df.with_columns(decade_expr()).group_by("ERA")
         .agg([pl.len().alias("_n")] + [e.fill_null(False).sum().alias(k) for k, e in ex.items()]).sort("ERA"))
    for r in g.iter_rows(named=True):
        out += [rec("financials", "anomaly", r["ERA"], k, r["_n"], r[k]) for k in ex]
    return out


def zero_records(df: pl.DataFrame) -> list[Rec]:
    """Exact-zero rates per field/era on RAW data (detects disguised missingness)."""
    cols = C.NUM_FIELDS
    out: list[Rec] = []
    tot = df.select([(pl.col(c) == 0).sum().alias(c) for c in cols]).row(0, named=True)
    out += [rec("financials", "exact_zero", "ALL", c, df.height, tot[c]) for c in cols]
    g = (df.with_columns(decade_expr()).group_by("ERA")
         .agg([pl.len().alias("_n")] + [(pl.col(c) == 0).sum().alias(c) for c in cols]).sort("ERA"))
    for r in g.iter_rows(named=True):
        out += [rec("financials", "exact_zero", r["ERA"], c, r["_n"], r[c]) for c in cols]
    return out


def duplicate_and_gap_records(df: pl.DataFrame) -> tuple[list[Rec], dict[str, Any]]:
    n_dup = df.height - df.unique(subset=C.KEY_FIELDS).height
    q = (df.select("CERT", "REPDATE").with_columns(
        Q=pl.col("REPDATE").dt.year() * 4 + (pl.col("REPDATE").dt.month() - 1) // 3)
        .sort("CERT", "Q"))
    q = q.with_columns(GAP=(pl.col("Q") - pl.col("Q").shift(1).over("CERT") - 1))
    gaps = q.filter(pl.col("GAP") > 0)
    info = {"duplicate_cert_repdte": int(n_dup), "n_gaps": gaps.height,
            "n_banks_with_gap": gaps["CERT"].n_unique(), "n_banks": q["CERT"].n_unique(),
            "max_gap_quarters": int(gaps["GAP"].max()) if gaps.height else 0,
            "total_missing_quarters": int(gaps["GAP"].sum()) if gaps.height else 0}
    recs = [rec("financials", "duplicate", "ALL", "CERT,REPDTE", df.height, n_dup),
            rec("financials", "reporting_gap", "ALL", "bank_gaps", q.height, gaps.height),
            rec("financials", "reporting_gap", "ALL", "banks_with_gap", info["n_banks"], info["n_banks_with_gap"])]
    return recs, info


def audit_small_tables(fail: pl.DataFrame, inst: pl.DataFrame) -> tuple[list[Rec], dict[str, Any]]:
    recs: list[Rec] = []
    recs += missingness_records(fail, "failures", fail.columns, None)
    recs += missingness_records(inst, "institutions", inst.columns, None)
    f_only = fail.filter((pl.col("RESTYPE") == "FAILURE") & pl.col("CERT").is_not_null())
    dup_f = f_only.height - f_only.unique("CERT").height
    both = fail.filter(pl.col("CERT").is_not_null()).group_by("CERT").agg(pl.col("RESTYPE").n_unique()).filter(pl.col("RESTYPE") > 1).height
    inst_dup = inst.height - inst.unique("CERT").height
    end_lt_est = inst.filter(pl.col("ENDEFYMD") < pl.col("ESTYMD")).height
    active_inconsistent = inst.filter((pl.col("ACTIVE") == 1) != (pl.col("ENDEFYMD").dt.year() == 9999)).height
    f_not_in_inst = fail.filter(pl.col("CERT").is_not_null()).join(inst.select("CERT"), on="CERT", how="anti").height
    recs += [
        rec("failures", "anomaly", "ALL", "restype_ASSISTANCE", fail.height, fail.filter(pl.col("RESTYPE") == "ASSISTANCE").height),
        rec("failures", "duplicate", "ALL", "CERT_within_FAILURE", f_only.height, dup_f),
        rec("failures", "anomaly", "ALL", "CERT_with_both_ASSISTANCE_and_FAILURE", fail["CERT"].n_unique(), both),
        rec("failures", "anomaly", "ALL", "CERT_not_in_institutions", fail.height, f_not_in_inst),
        rec("institutions", "duplicate", "ALL", "CERT", inst.height, inst_dup),
        rec("institutions", "anomaly", "ALL", "ENDEFYMD_before_ESTYMD", inst.height, end_lt_est),
        rec("institutions", "anomaly", "ALL", "ACTIVE_vs_ENDEFYMD_inconsistent", inst.height, active_inconsistent),
    ]
    return recs, {"failures_rows": fail.height, "failure_restype_rows": f_only.height,
                  "institutions_rows": inst.height}


# ----------------------------------------------------------------------------
# Point-in-time panel
# ----------------------------------------------------------------------------
def failure_dates(fail: pl.DataFrame) -> pl.DataFrame:
    """One row per CERT: earliest FAILURE date (ASSISTANCE is not a failure)."""
    return (fail.filter((pl.col("RESTYPE") == "FAILURE") & pl.col("CERT").is_not_null())
            .group_by("CERT").agg(pl.col("FAILDATE").min().alias("META_FAILDATE")))


def build_panel(fin: pl.DataFrame, fail: pl.DataFrame, inst: pl.DataFrame) -> tuple[pl.DataFrame, list[Rec]]:
    log: list[Rec] = []
    n0 = fin.height

    def lg(rule: str, desc: str, action: str, affected: int, before: int, after: int, note: str = "") -> None:
        log.append({"rule": rule, "description": desc, "action": action, "rows_before": before,
                    "rows_affected": affected, "rows_after": after, "note": note})

    # R1: exact duplicate keys -> keep first (deterministic order by CERT, REPDTE)
    fin = fin.sort(C.KEY_FIELDS)
    dedup = fin.unique(subset=C.KEY_FIELDS, keep="first", maintain_order=True)
    lg("R1_duplicates", "duplicate (CERT,REPDTE)", "drop", fin.height - dedup.height, fin.height, dedup.height)
    fin = dedup

    # R3: disguised missing (pre-Basel zeros) -> NaN, calendar rule, row-local
    fin, n_mask = mask_disguised_missing(fin)
    lg("R3_basel_zero_to_nan", f"{'/'.join(C.BASEL_FIELDS)} == 0 before {C.BASEL_START} -> NaN",
       "set_nan", n_mask, fin.height, fin.height, "cell count; rows kept")

    # Joins (point-in-time safe: static attributes only; exit info goes to META_/LABEL_ columns)
    fd = failure_dates(fail)
    inst_cols = inst.select("CERT", "BKCLASS", "ESTYMD", pl.col("ACTIVE").alias("_ACTIVE"),
                            pl.col("ENDEFYMD").alias("META_ENDEFYMD"), pl.col("CHANGEC1").alias("META_CHANGEC1"))
    df = fin.join(inst_cols, on="CERT", how="left").join(fd, on="CERT", how="left")
    df = df.with_columns(
        META_EXIT_CLASS=pl.when(pl.col("META_FAILDATE").is_not_null()).then(pl.lit("FAILURE"))
        .when(pl.col("_ACTIVE") == 0).then(pl.lit("MERGER_OR_VOLUNTARY"))
        .when(pl.col("_ACTIVE") == 1).then(pl.lit("ACTIVE"))
        .otherwise(pl.lit("UNKNOWN")),
        BANK_AGE_Y=(pl.col("REPDATE") - pl.col("ESTYMD")).dt.total_days() / 365.25,
    ).drop("_ACTIVE", "ESTYMD")
    df = df.with_columns(pl.when(pl.col("BANK_AGE_Y") < 0).then(None).otherwise(pl.col("BANK_AGE_Y")).alias("BANK_AGE_Y"))

    # Target: failure strictly after REPDTE and within 4 quarters
    df = df.with_columns(
        LABEL_Y=((pl.col("META_FAILDATE") > pl.col("REPDATE")) &
                 (pl.col("META_FAILDATE") <= pl.col("REPDATE").dt.offset_by(C.LABEL_HORIZON)))
        .fill_null(False).cast(pl.Int8),
        FLAG_POST_FAILURE_ROW=(pl.col("META_FAILDATE") <= pl.col("REPDATE")).fill_null(False),
        SPLIT=pl.when(pl.col("REPDATE") <= pl.lit(C.DEV_END).str.to_date()).then(pl.lit("DEV"))
        .when(pl.col("REPDATE") < pl.lit(C.TEST_START).str.to_date()).then(pl.lit("GAP"))
        .when(pl.col("REPDATE") <= pl.lit(C.MODEL_END).str.to_date()).then(pl.lit("TEST"))
        .otherwise(pl.lit("POST")),
    ).with_columns(LABEL_OBSERVABLE=pl.col("SPLIT") != "POST")

    n_post = int(df["FLAG_POST_FAILURE_ROW"].sum())
    lg("F1_post_failure_rows", "REPDTE >= failure date (reporting after failure); kept, flagged, y=0",
       "flag", n_post, df.height, df.height, "decide exclusion in modelling stage")

    # R2: non-positive / null assets -> drop (row-local; ratios undefined)
    bad = (pl.col("ASSET").is_null() | (pl.col("ASSET") <= 0))
    dropped = df.filter(bad)
    kept = df.filter(~bad)
    lg("R2_asset_nonpositive", "ASSET null or <= 0", "drop", dropped.height, df.height, kept.height,
       f"positives (y=1) among dropped rows: {int(dropped['LABEL_Y'].sum())}")
    df = kept

    # Flags (row-local, rows kept)
    ex = anomaly_exprs()
    flag_map = {"FLAG_DEP_NEG": "DEP_lt_0", "FLAG_LNLSNET_GT_GROSS": "LNLSNET_gt_LNLSGR",
                "FLAG_RESERVES_NEG": "LNATRES_lt_0", "FLAG_EQ_ASSET_EXTREME": "EQ_ASSET_extreme",
                "FLAG_EQ_NEG": "EQ_lt_0"}
    df = df.with_columns([ex[v].fill_null(False).alias(k) for k, v in flag_map.items()])
    for k, v in flag_map.items():
        lg(f"F_{v}", f"{v} (flag only; values untouched)", "flag", int(df[k].sum()), df.height, df.height)

    # N1: structural NaNs preserved
    nulls = int(df.select(pl.sum_horizontal([pl.col(c).is_null().cast(pl.Int64) for c in C.NUM_FIELDS])).sum().item())
    lg("N1_structural_nan", "NaNs in numeric fields (e.g. pre-1990 Basel ratios) preserved for in-pipeline imputation",
       "keep", nulls, df.height, df.height, "cell count, not rows")
    lg("TOTAL", "rows input -> panel", "summary", n0 - df.height, n0, df.height)

    df = df.sort(C.KEY_FIELDS).drop("YEAR").with_columns(YEAR=pl.col("REPDATE").dt.year().cast(pl.Int32))
    return df, log


# ----------------------------------------------------------------------------
# Figures
# ----------------------------------------------------------------------------
def fig_missingness(by_year: pl.DataFrame) -> Path:
    pdf = by_year.to_pandas().set_index("YEAR").drop(columns="n_rows").T
    fig, ax = plt.subplots(figsize=(20, 10))
    sns.heatmap(pdf, cmap="viridis_r", vmin=0, vmax=1, ax=ax, cbar_kws={"label": "missing rate"},
                linewidths=0.2, linecolor="white")
    ax.set_title("Missingness rate by field and year (FDIC Call Reports, 1984-2026)")
    ax.set_xlabel("Year")
    ax.set_ylabel("Field")
    fig.tight_layout()
    path = C.FIGURES / "fig1_missingness_by_era.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def fig_panel_failures(panel: pl.DataFrame, fail: pl.DataFrame) -> Path:
    act = panel.group_by("REPDATE").agg(pl.len().alias("n")).sort("REPDATE").to_pandas()
    f = (failure_dates(fail).with_columns(
        Q=pl.col("META_FAILDATE").dt.truncate("1q").dt.offset_by("3mo").dt.offset_by("-1d"))
        .filter((pl.col("META_FAILDATE") >= pl.lit(C.FULL_START).str.to_date()) &
                (pl.col("META_FAILDATE") <= pl.lit(C.FULL_END).str.to_date()))
        .group_by("Q").agg(pl.len().alias("n")).sort("Q").to_pandas())
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    a1.plot(act["REPDATE"], act["n"], color="navy")
    a1.set_ylabel("Reporting banks")
    a2.bar(f["Q"], f["n"], width=70, color="firebrick")
    a2.set_ylabel("Failures per quarter")
    spans = [("1984-01-01", C.DEV_END, "tab:blue", "Dev 1984Q1-2006Q4"),
             ("2007-01-01", "2007-12-31", "tab:gray", "4Q gap 2007Q1-2007Q4"),
             (C.TEST_START[:4] + "-01-01", C.MODEL_END, "tab:orange", "Test 2008Q1-2025Q2")]
    for ax in (a1, a2):
        for s, e, col, lab in spans:
            ax.axvspan(np.datetime64(s), np.datetime64(e), color=col, alpha=0.12,
                       label=lab if ax is a1 else None)
    a1.legend(loc="upper right")
    a1.set_title("Active banks and quarterly failures with Dev / Gap / Test split")
    fig.tight_layout()
    path = C.FIGURES / "fig2_failures_and_panel_over_time.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ----------------------------------------------------------------------------
def split_report(panel: pl.DataFrame) -> pl.DataFrame:
    return (panel.group_by("SPLIT").agg(
        pl.len().alias("rows"), pl.col("LABEL_Y").sum().alias("positive_rows_y1"),
        pl.col("CERT").n_unique().alias("distinct_banks"),
        pl.col("CERT").filter(pl.col("LABEL_Y") == 1).n_unique().alias("distinct_failing_banks_y1"),
        pl.col("META_FAILDATE").filter(pl.col("META_FAILDATE").is_not_null()).n_unique().alias("_x"),
        pl.col("REPDATE").min().alias("first_repdate"), pl.col("REPDATE").max().alias("last_repdate"))
        .drop("_x").with_columns((pl.col("positive_rows_y1") / pl.col("rows")).alias("prevalence"))
        .sort("first_repdate"))


def main() -> None:
    for d in (C.TABLES, C.REPORTS, C.FIGURES, C.DATA_PROCESSED):
        d.mkdir(parents=True, exist_ok=True)

    fin, fail, inst = load_financials(), load_failures(), load_institutions()
    print(f"loaded: financials={fin.height:,} failures={fail.height:,} institutions={inst.height:,}")

    # ---- audit
    recs: list[Rec] = []
    recs += zero_records(fin)
    fin_eff, n_masked = mask_disguised_missing(fin)  # effective missingness (Basel zeros -> NaN)
    print(f"disguised-missing cells (pre-{C.BASEL_START} Basel ratios == 0): {n_masked:,}")
    recs += missingness_records(fin_eff, "financials", C.FIN_FIELDS[2:], decade_expr())
    by_year = missingness_by_year(fin_eff)
    by_year.write_csv(C.TABLES / "part2_missingness_by_year.csv")
    recs += anomaly_records(fin)
    dg_recs, dg_info = duplicate_and_gap_records(fin)
    recs += dg_recs
    small_recs, small_info = audit_small_tables(fail, inst)
    recs += small_recs
    summary = pl.DataFrame(recs)
    summary.write_csv(C.TABLES / "part2_dq_summary.csv")

    # ---- panel + cleaning
    panel, clog = build_panel(fin, fail, inst)
    pl.DataFrame(clog).write_csv(C.TABLES / "part2_cleaning_log.csv")
    panel.write_parquet(C.CLEAN_PANEL, compression="zstd")
    split = split_report(panel)
    split.write_csv(C.TABLES / "part2_split_summary.csv")
    exit_counts = panel.group_by("META_EXIT_CLASS").agg(pl.len().alias("rows"), pl.col("CERT").n_unique().alias("banks")).sort("rows", descending=True)

    audit = {"row_counts": {"financials": fin.height, "panel": panel.height, **small_info},
             "date_range": [str(fin["REPDATE"].min()), str(fin["REPDATE"].max())],
             "duplicates_and_gaps": dg_info, "structural_shifts": structural_shifts(by_year),
             "anomalies_all": [r for r in recs if r["check"] == "anomaly" and r["group"] == "ALL"],
             "missingness_all": [r for r in recs if r["check"] == "missing" and r["group"] == "ALL"],
             "split_summary": split.with_columns(pl.col(pl.Date).cast(pl.Utf8)).to_dicts(),
             "exit_classes": exit_counts.to_dicts(),
             "cleaning_log": clog}
    (C.REPORTS / "dq_audit.json").write_text(json.dumps(audit, indent=1, default=str))

    figs = [fig_missingness(by_year), fig_panel_failures(panel, fail)]

    # ---- print
    all_rows = summary.filter(pl.col("group") == "ALL")
    print("\n=== DQ summary: financials missingness (ALL, >0) ===")
    print(all_rows.filter((pl.col("table") == "financials") & (pl.col("check") == "missing") & (pl.col("n_flagged") > 0))
          .select("field", "n_flagged", "rate").sort("rate", descending=True))
    print("\n=== DQ summary: anomalies / duplicates / gaps (ALL) ===")
    print(all_rows.filter(pl.col("check") != "missing").select("table", "check", "field", "n_total", "n_flagged", "rate"))
    print("\n=== Structural shifts (first year field is >=50% populated) ===")
    for k, v in audit["structural_shifts"].items():
        if v["structural_shift"]:
            print(f"  {k:<8} first_year={v['first_year_available']}  yearly missing range "
                  f"{v['min_yearly_missing']:.2f}-{v['max_yearly_missing']:.2f}")
    print("\n=== Gaps ===", dg_info)
    print("\n=== Cleaning log ===")
    print(pl.DataFrame(clog).select("rule", "action", "rows_before", "rows_affected", "rows_after", "note"))
    print("\n=== Exit classes ===")
    print(exit_counts)
    print("\n=== Dev vs Gap vs Test (rows / failures y=1) ===")
    print(split)
    print(f"\npanel: {panel.height:,} rows x {panel.width} cols -> {C.CLEAN_PANEL}")
    print("\nFiles in outputs/:")
    for p in sorted(C.OUTPUTS.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(C.ROOT)}  ({p.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
