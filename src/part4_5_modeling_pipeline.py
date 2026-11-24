"""Part 4 (leak-free CAMELS feature pipeline) + Part 5 (temporal validation, models, cost threshold).

Split rules : Dev 1984Q1-2006Q4 | Embargo 2007Q1-2007Q4 (excluded) | Test 2008Q1-2025Q2 (evaluated ONCE).
Excluded    : FLAG_POST_FAILURE_ROW rows, REPDTE > MODEL_END.
Never features: META_* , LABEL_Y, FLAG_* (not even loaded into the feature builder).

Run: python src/part4_5_modeling_pipeline.py   (outside sandbox: Spark MLlib needs a local socket)
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import polars as pl  # noqa: E402
from lightgbm import LGBMClassifier  # noqa: E402
from sklearn.base import BaseEstimator, OneToOneFeatureMixin, TransformerMixin  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import (average_precision_score, brier_score_loss, precision_recall_curve,  # noqa: E402
                             roc_auc_score, roc_curve)
from sklearn.model_selection import ParameterSampler  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from sklearn.utils.validation import check_is_fitted  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")
pl.enable_string_cache()
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
RAW = ["CERT", "REPDTE", "ASSET", "DEP", "DEPDOM", "EQ", "BRO", "CHBAL", "SC", "LNLSGR", "LNLSNET",
       "LNRE", "LNCI", "LNATRES", "NCLNLS", "P9ASSET", "NAASSET", "NTLNLS", "ORE", "NONIX", "INTINC",
       "EINTEXP", "NONII", "ELNATR", "ROA", "ROE", "NIMY", "RBC1AAJ", "RBCRWAJ"]
PEER_VARS = ["CAP", "NPL", "ROA", "LTD", "LOG_ASSET"]
FEATURES = (["CAP", "RBC1AAJ", "RBCRWAJ", "CAP_QOQ"]                                           # C
            + ["NPL", "P9_ASSET", "NAA_ASSET", "ORE_ASSET", "RES_NPL", "NCO_LOANS"]            # A
            + ["EFF", "ASSET_G4", "LOAN_G4", "BRO_DEP"]                                        # M
            + ["ROA", "ROE", "NIMY", "ELNATR_ASSET", "ROA_MEAN4", "ROA_STD4"]                  # E
            + ["CHBAL_ASSET", "SC_ASSET", "LTD", "DEPDOM_DEP"]                                 # L
            + ["LOG_ASSET", "LNRE_LOANS", "LNCI_LOANS"] + [f"{v}_Z" for v in PEER_VARS])       # S
FORBIDDEN_PREFIXES = ("META_", "LABEL", "FLAG_", "FAIL", "SPLIT")
KEYS = ["CERT", "REPDTE"]

C_FP_BASE_K = 100.0          # ASSUMPTION: $100k supervisory inspection cost per flagged bank-quarter
RATIO_GRID = [10, 25, 50, 100, 200, 500, 1000]  # C_FN_row / C_FP sensitivity grid
VAL_BLOCKS = [("1990-03-31", "1992-12-31"), ("1993-03-31", "1995-12-31"), ("1996-03-31", "1998-12-31"),
              ("1999-03-31", "2002-12-31"), ("2003-03-31", "2006-12-31")]
EMBARGO_Q = 4
OUT = C.OUTPUTS
PART5_DIR = C.DATA_PROCESSED / "part5"


def log(msg: str) -> None:
    print(msg, flush=True)


def qidx(d: pd.Series | np.ndarray) -> np.ndarray:
    """Quarter index from YYYYMMDD ints."""
    d = np.asarray(d, dtype=np.int64)
    return (d // 10000) * 4 + ((d // 100) % 100 - 1) // 3


def date_to_int(s: str) -> int:
    return int(s.replace("-", ""))


# ----------------------------------------------------------------------------
# Part 4a - feature engineering (Polars, backward-looking only)
# ----------------------------------------------------------------------------
def build_features(raw: pl.DataFrame) -> pl.DataFrame:
    """CAMELS features. Inputs: RAW columns only (no label / META / FLAG). Uses only the row itself,
    that bank's PAST quarters (gap-safe quarter index) and same-REPDTE cross-section."""
    c = pl.col
    lf = (raw.lazy().select(RAW)
          .with_columns([c(x).cast(pl.Float64) for x in RAW if x not in KEYS]
                        + [c("CERT").cast(pl.Int64), c("REPDTE").cast(pl.Int64)])
          .sort(KEYS))

    def sd(n: pl.Expr, d: pl.Expr) -> pl.Expr:
        return pl.when(d > 0).then(n / d)

    lf = lf.with_columns(
        CAP=sd(c("EQ"), c("ASSET")),
        NPL=sd(c("NCLNLS"), c("LNLSGR")), P9_ASSET=sd(c("P9ASSET"), c("ASSET")),
        NAA_ASSET=sd(c("NAASSET"), c("ASSET")), ORE_ASSET=sd(c("ORE"), c("ASSET")),
        RES_NPL=sd(c("LNATRES"), c("NCLNLS")), NCO_LOANS=sd(c("NTLNLS"), c("LNLSGR")),
        EFF=sd(c("NONIX"), c("INTINC") - c("EINTEXP") + c("NONII")), BRO_DEP=sd(c("BRO"), c("DEP")),
        ELNATR_ASSET=sd(c("ELNATR"), c("ASSET")),
        CHBAL_ASSET=sd(c("CHBAL"), c("ASSET")), SC_ASSET=sd(c("SC"), c("ASSET")),
        LTD=sd(c("LNLSNET"), c("DEP")), DEPDOM_DEP=sd(c("DEPDOM"), c("DEP")),
        LOG_ASSET=pl.when(c("ASSET") > 0).then(c("ASSET").log()),
        LNRE_LOANS=sd(c("LNRE"), c("LNLSGR")), LNCI_LOANS=sd(c("LNCI"), c("LNLSGR")),
        QIDX=(c("REPDTE") // 10000) * 4 + ((c("REPDTE") // 100) % 100 - 1) // 3,
    )
    q = c("QIDX")
    ok = {k: (q - q.shift(k).over("CERT")) == k for k in (1, 3, 4)}
    la, ll = c("ASSET").shift(4).over("CERT"), c("LNLSGR").shift(4).over("CERT")
    lf = lf.with_columns(
        CAP_QOQ=pl.when(ok[1]).then(c("CAP") - c("CAP").shift(1).over("CERT")),
        ASSET_G4=pl.when(ok[4] & (la > 0)).then(c("ASSET") / la - 1),
        LOAN_G4=pl.when(ok[4] & (ll > 0)).then(c("LNLSGR") / ll - 1),
        ROA_MEAN4=pl.when(ok[3]).then(c("ROA").rolling_mean(window_size=4, min_periods=4).over("CERT")),
        ROA_STD4=pl.when(ok[3]).then(c("ROA").rolling_std(window_size=4, min_periods=4).over("CERT")),
    )
    lf = lf.with_columns([c(v).mean().over("REPDTE").alias(f"_{v}_m") for v in PEER_VARS]
                         + [c(v).std().over("REPDTE").alias(f"_{v}_s") for v in PEER_VARS])
    lf = lf.with_columns([pl.when(c(f"_{v}_s") > 1e-9).then((c(v) - c(f"_{v}_m")) / c(f"_{v}_s")).alias(f"{v}_Z")
                          for v in PEER_VARS])
    return lf.select(KEYS + FEATURES).collect()


# ----------------------------------------------------------------------------
# Part 4b - preprocessing (fit on train only, inside every fold)
# ----------------------------------------------------------------------------
class Winsorizer(OneToOneFeatureMixin, TransformerMixin, BaseEstimator):
    """Clip each column to [lower, upper] quantiles learned ONLY from the data passed to fit."""

    def __init__(self, lower: float = 0.005, upper: float = 0.995) -> None:
        self.lower, self.upper = lower, upper

    def fit(self, X: Any, y: Any = None) -> "Winsorizer":
        X = self._validate_data(X, dtype=np.float64, force_all_finite="allow-nan")
        lo, hi = np.nanquantile(X, [self.lower, self.upper], axis=0)
        self.lo_ = np.where(np.isnan(lo), -np.inf, lo)
        self.hi_ = np.where(np.isnan(hi), np.inf, hi)
        self.n_fit_ = X.shape[0]
        return self

    def transform(self, X: Any) -> np.ndarray:
        check_is_fitted(self, ["lo_", "hi_"])
        X = self._validate_data(X, dtype=np.float64, force_all_finite="allow-nan", reset=False)
        return np.clip(X, self.lo_, self.hi_)  # NaN preserved for the imputer


def make_preprocessor() -> Pipeline:
    return Pipeline([("winsor", Winsorizer()),
                     ("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                     ("scale", StandardScaler())])


def make_model(clf: Any) -> Pipeline:
    return Pipeline([("prep", make_preprocessor()), ("clf", clf)])


def make_lr() -> LogisticRegression:
    return LogisticRegression(class_weight="balanced", C=1.0, max_iter=300, solver="lbfgs")


def make_lgbm(**p: Any) -> LGBMClassifier:
    return LGBMClassifier(random_state=C.SEED, n_jobs=os.cpu_count(), subsample=0.8, subsample_freq=1,
                          verbose=-1, **p)


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
def recall_at_top(y: np.ndarray, p: np.ndarray, frac: float) -> float:
    k = max(1, int(np.ceil(frac * len(y))))
    top = np.argpartition(-p, k - 1)[:k]
    return float(y[top].sum() / max(y.sum(), 1))


def metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    if y.sum() == 0:
        return {k: np.nan for k in ("pr_auc", "roc_auc", "recall_top1", "recall_top5", "brier")}
    return {"pr_auc": average_precision_score(y, p), "roc_auc": roc_auc_score(y, p),
            "recall_top1": recall_at_top(y, p, 0.01), "recall_top5": recall_at_top(y, p, 0.05),
            "brier": brier_score_loss(y, np.clip(p, 0, 1))}


# ----------------------------------------------------------------------------
# Data assembly
# ----------------------------------------------------------------------------
def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pl.DataFrame]:
    """Returns (dev, test, raw_all_rows). dev/test: KEYS + FEATURES + y."""
    cols = RAW + ["REPDATE", "LABEL_Y", "FLAG_POST_FAILURE_ROW"]
    panel = pl.read_parquet(C.CLEAN_PANEL, columns=cols)
    raw = panel.select(RAW)                       # feature builder never sees label / flags
    feats = build_features(raw)
    meta = panel.select("CERT", "REPDTE", "REPDATE", "LABEL_Y", "FLAG_POST_FAILURE_ROW").with_columns(
        pl.col("CERT").cast(pl.Int64), pl.col("REPDTE").cast(pl.Int64))
    df = feats.join(meta, on=KEYS, how="inner")
    assert df.height == panel.height, "feature/meta join changed the row count"
    n0 = df.height
    df = df.filter(~pl.col("FLAG_POST_FAILURE_ROW") & (pl.col("REPDATE") <= pl.lit(C.MODEL_END).str.to_date()))
    log(f"panel rows {n0:,} -> after excluding post-failure & post-{C.MODEL_END}: {df.height:,}")
    dev_end, test_start = date_to_int(C.DEV_END), date_to_int(C.TEST_START)
    dev = df.filter(pl.col("REPDTE") <= dev_end)
    test = df.filter(pl.col("REPDTE") >= test_start)
    log(f"embargo rows dropped: {df.height - dev.height - test.height:,}")
    keep = KEYS + FEATURES + ["LABEL_Y"]
    to_pd = lambda d: d.select(keep).rename({"LABEL_Y": "y"}).to_pandas()  # noqa: E731
    return to_pd(dev).reset_index(drop=True), to_pd(test).reset_index(drop=True), raw


# ----------------------------------------------------------------------------
# Leakage unit tests
# ----------------------------------------------------------------------------
class LeakageReport:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        self.rows.append({"check": name, "passed": bool(cond), "detail": detail})
        assert cond, f"LEAKAGE CHECK FAILED: {name} {detail}"

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def make_folds(dev: pd.DataFrame) -> list[tuple[np.ndarray, np.ndarray, str]]:
    q = qidx(dev["REPDTE"].to_numpy())
    folds = []
    for vs, ve in VAL_BLOCKS:
        qv0, qv1 = qidx([date_to_int(vs)])[0], qidx([date_to_int(ve)])[0]
        train = np.where(q <= qv0 - EMBARGO_Q - 1)[0]      # 4 quarters (qv0-4..qv0-1) are embargoed
        val = np.where((q >= qv0) & (q <= qv1))[0]
        folds.append((train, val, f"{vs[:4]}Q{(int(vs[5:7]) - 1) // 3 + 1}-{ve[:4]}Q{(int(ve[5:7]) - 1) // 3 + 1}"))
    return folds


def leakage_tests_static(dev: pd.DataFrame, test: pd.DataFrame, raw: pl.DataFrame,
                         folds: list[tuple[np.ndarray, np.ndarray, str]], rep: LeakageReport) -> None:
    bad = [f for f in FEATURES if f.startswith(FORBIDDEN_PREFIXES) or f in ("y", "LABEL_Y")]
    rep.check("features_have_no_META_LABEL_FLAG_columns", not bad, f"{len(FEATURES)} features")
    rep.check("feature_builder_input_has_no_label_or_meta", not [c for c in RAW if c.startswith(FORBIDDEN_PREFIXES)])
    X = dev[FEATURES]
    rep.check("X_columns_equal_FEATURES", list(X.columns) == FEATURES)
    rep.check("X_has_no_key_or_target_columns", not ({"y", "CERT", "REPDTE"} & set(X.columns)))
    q_dev_max, q_test_min = qidx([dev["REPDTE"].max()])[0], qidx([test["REPDTE"].min()])[0]
    rep.check("max_dev_date_plus_4Q_lt_min_test_date", q_dev_max + 4 < q_test_min,
              f"dev_max={dev['REPDTE'].max()} test_min={test['REPDTE'].min()} gap_q={q_test_min - q_dev_max - 1}")
    rep.check("dev_max_le_DEV_END", dev["REPDTE"].max() <= date_to_int(C.DEV_END))
    rep.check("test_range_ok", test["REPDTE"].min() >= date_to_int(C.TEST_START)
              and test["REPDTE"].max() <= date_to_int(C.MODEL_END))
    rep.check("no_embargo_rows_in_dev_or_test", not (
        dev["REPDTE"].between(20070101, 20071231).any() or test["REPDTE"].between(20070101, 20071231).any()))
    rep.check("no_row_overlap_dev_test", set(dev["REPDTE"]).isdisjoint(set(test["REPDTE"])))
    qd = qidx(dev["REPDTE"].to_numpy())
    for i, (tr, va, name) in enumerate(folds, 1):
        gap = qd[va].min() - qd[tr].max() - 1
        rep.check(f"fold{i}_{name}_embargo_ge_4Q", gap >= EMBARGO_Q, f"gap_quarters={gap}")
        rep.check(f"fold{i}_train_val_disjoint", np.intersect1d(tr, va).size == 0)
        rep.check(f"fold{i}_train_label_horizon_before_val", qd[tr].max() + 4 < qd[va].min())
    rep.check("expanding_window_train_sizes_increase", all(
        len(folds[i][0]) < len(folds[i + 1][0]) for i in range(len(folds) - 1)))
    # Backward-looking proof: features of rows <= cutoff are unchanged when ALL later data is deleted.
    cutoff = 20001231
    full = build_features(raw).filter(pl.col("REPDTE") <= cutoff).sort(KEYS)
    trunc = build_features(raw.filter(pl.col("REPDTE") <= cutoff)).sort(KEYS)
    same = full.height == trunc.height and all(
        np.array_equal(full[f].to_numpy(), trunc[f].to_numpy(), equal_nan=True) or
        np.allclose(full[f].to_numpy(), trunc[f].to_numpy(), rtol=0, atol=1e-12, equal_nan=True) for f in FEATURES)
    rep.check("features_unchanged_when_future_data_deleted(2000-12-31 cutoff)", same, f"rows={full.height:,}")


def leakage_check_fold_fit(prep: Pipeline, Xtr: pd.DataFrame, tr_idx: np.ndarray, fold: int,
                           rep: LeakageReport) -> None:
    w: Winsorizer = prep.named_steps["winsor"]
    rep.check(f"fold{fold}_winsor_fit_rows_equal_train_rows", w.n_fit_ == len(tr_idx), f"n_fit={w.n_fit_:,}")
    rng = np.random.default_rng(C.SEED + fold)
    cols = rng.choice(len(FEATURES), size=3, replace=False)
    arr = Xtr.to_numpy(dtype=np.float64)
    ok = all(np.isclose(np.nanquantile(arr[:, j], 0.005), w.lo_[j], equal_nan=True)
             and np.isclose(np.nanquantile(arr[:, j], 0.995), w.hi_[j], equal_nan=True)
             or np.isinf(w.lo_[j]) for j in cols)
    rep.check(f"fold{fold}_winsor_bounds_equal_train_only_quantiles", ok, f"cols={[FEATURES[j] for j in cols]}")
    imp: SimpleImputer = prep.named_steps["impute"]
    med = np.nanmedian(np.clip(arr, w.lo_, w.hi_), axis=0)       # full fold-train set, winsorized
    med = np.where(np.isnan(med), 0.0, med)                      # keep_empty_features=True fills all-NaN columns with 0
    rep.check(f"fold{fold}_imputer_fitted_on_train_stats_only",
              bool(np.allclose(imp.statistics_, med, equal_nan=True)),
              "imputer medians == nanmedian(clip(train)) over all train rows")


# ----------------------------------------------------------------------------
# Part 5a - temporal CV
# ----------------------------------------------------------------------------
def run_cv(dev: pd.DataFrame, folds: list[tuple[np.ndarray, np.ndarray, str]], rep: LeakageReport
           ) -> tuple[pd.DataFrame, dict[str, np.ndarray], dict[str, Any]]:
    X, y = dev[FEATURES], dev["y"].to_numpy()
    cfgs = list(ParameterSampler(
        {"num_leaves": [15, 31, 63], "learning_rate": [0.03, 0.05, 0.1], "n_estimators": [200, 400],
         "min_child_samples": [50, 200, 500], "colsample_bytree": [0.6, 0.8, 1.0], "reg_lambda": [0.0, 1.0, 10.0]},
        n_iter=6, random_state=C.SEED))
    models: dict[str, Any] = {"LogReg_balanced": make_lr}
    for i, p in enumerate(cfgs):
        models[f"LightGBM_cfg{i}"] = (lambda p=p: make_lgbm(**p))
    rows: list[dict[str, Any]] = []
    val_all = np.concatenate([v for _, v, _ in folds])
    oof = {m: np.full(len(val_all), np.nan) for m in models}
    offs = np.cumsum([0] + [len(v) for _, v, _ in folds])
    for fi, (tr, va, name) in enumerate(folds, 1):
        t0 = time.time()
        prep = make_preprocessor().fit(X.iloc[tr])          # fitted on TRAIN of this fold only
        leakage_check_fold_fit(prep, X.iloc[tr], tr, fi, rep)
        Xtr, Xva = prep.transform(X.iloc[tr]), prep.transform(X.iloc[va])
        for m, mk in models.items():
            clf = mk().fit(Xtr, y[tr])
            p = clf.predict_proba(Xva)[:, 1]
            oof[m][offs[fi - 1]:offs[fi]] = p
            rows.append({"model": m, "fold": fi, "val_period": name, "n_train": len(tr), "n_val": len(va),
                         "n_pos_val": int(y[va].sum()), **metrics(y[va], p)})
        log(f"  fold {fi} ({name}) train={len(tr):,} val={len(va):,} pos={int(y[va].sum())}  {time.time() - t0:.0f}s")
    cv = pd.DataFrame(rows)
    pooled = [{"model": m, "fold": "pooled_oof", "val_period": "1990Q1-2006Q4", "n_train": np.nan,
               "n_val": len(val_all), "n_pos_val": int(y[val_all].sum()), **metrics(y[val_all], oof[m])}
              for m in models]
    mean = (cv.groupby("model")[["pr_auc", "roc_auc", "recall_top1", "recall_top5", "brier"]].mean()
            .reset_index().assign(fold="mean_over_folds"))
    cv = pd.concat([cv, mean, pd.DataFrame(pooled)], ignore_index=True)
    lg = cv[(cv["fold"] == "mean_over_folds") & cv["model"].str.startswith("LightGBM")]
    best = lg.sort_values("pr_auc", ascending=False).iloc[0]["model"]
    best_params = cfgs[int(best.replace("LightGBM_cfg", ""))]
    cv.insert(1, "params", cv["model"].map(
        lambda m: json.dumps(cfgs[int(m.replace("LightGBM_cfg", ""))]) if m.startswith("LightGBM") else "balanced,C=1"))
    cv["selected"] = cv["model"].isin(["LogReg_balanced", best])
    oof_final = {"LogReg_balanced": oof["LogReg_balanced"], "LightGBM_tuned": oof[best]}
    return cv, oof_final, {"best_name": best, "best_params": best_params, "val_idx": val_all}


# ----------------------------------------------------------------------------
# Part 5b - data-driven cost threshold
# ----------------------------------------------------------------------------
def estimate_costs(dev: pd.DataFrame) -> dict[str, float]:
    """C_FN from FDIC COST (thousands USD), failures dated within the DEV period only."""
    f = pl.read_csv(C.FAILURES_CSV, infer_schema_length=0).filter(
        (pl.col("RESTYPE") == "FAILURE") & pl.col("CERT").is_not_null() & pl.col("COST").is_not_null()
    ).with_columns(d=pl.col("FAILDATE").str.to_date("%m/%d/%Y", strict=False),
                   cost=pl.col("COST").cast(pl.Float64, strict=False))
    f = f.filter((pl.col("d") >= pl.date(1984, 1, 1)) & (pl.col("d") <= pl.lit(C.DEV_END).str.to_date()))
    cost = f["cost"].drop_nulls()
    pos = dev[dev["y"] == 1]
    k = len(pos) / max(pos["CERT"].nunique(), 1)       # positive bank-quarters per failing bank
    out = {"n_dev_failures_with_cost": float(len(cost)), "c_fn_mean_k": float(cost.mean()),
           "c_fn_median_k": float(cost.median()), "pos_rows_per_failure": float(k),
           "c_fn_row_k": float(cost.mean() / k), "c_fp_k": C_FP_BASE_K}
    out["base_ratio"] = out["c_fn_row_k"] / out["c_fp_k"]
    return out


def best_threshold(y: np.ndarray, p: np.ndarray, c_fn: float, c_fp: float) -> dict[str, float]:
    order = np.argsort(-p, kind="stable")
    ys, ps = y[order], p[order]
    P, n = ys.sum(), len(ys)
    tp = np.cumsum(ys)
    k = np.arange(1, n + 1)
    last = np.r_[ps[1:] != ps[:-1], True]               # evaluate at the end of tie groups
    cost = c_fn * (P - tp) + c_fp * (k - tp)
    cost_none = c_fn * P
    idx = np.where(last)[0]
    j = idx[np.argmin(cost[idx])]
    if cost[j] >= cost_none:
        return {"t_star": 1.01, "flag_rate": 0.0, "recall": 0.0, "precision": np.nan, "cost": cost_none,
                "cost_none": cost_none, "n": n}
    return {"t_star": float(ps[j]), "flag_rate": float(k[j] / n), "recall": float(tp[j] / max(P, 1)),
            "precision": float(tp[j] / k[j]), "cost": float(cost[j]), "cost_none": float(cost_none), "n": n}


def cost_curve(y: np.ndarray, p: np.ndarray, c_fn: float, c_fp: float, grid: int = 400
               ) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(-p, kind="stable")
    ys = y[order]
    P, n = ys.sum(), len(ys)
    tp = np.cumsum(ys)
    ks = np.unique(np.clip(np.geomspace(1, n, grid).astype(int), 1, n))
    cost = c_fn * (P - tp[ks - 1]) + c_fp * (ks - tp[ks - 1])
    return ks / n, cost / n * 1000


def threshold_sensitivity(y: np.ndarray, oof: dict[str, np.ndarray], costs: dict[str, float]
                          ) -> tuple[pd.DataFrame, dict[str, float]]:
    rows, base_t = [], {}
    scen = [("data_driven", costs["base_ratio"])] + [(f"ratio_{r}", float(r)) for r in RATIO_GRID]
    for m, p in oof.items():
        for name, ratio in scen:
            c_fp = costs["c_fp_k"]
            r = best_threshold(y, p, ratio * c_fp, c_fp)
            rows.append({"model": m, "scenario": name, "cfn_cfp_ratio": round(ratio, 2),
                         "c_fn_row_k": round(ratio * c_fp, 1), "c_fp_k": c_fp, "t_star": r["t_star"],
                         "flag_rate": r["flag_rate"], "recall": r["recall"], "precision": r["precision"],
                         "exp_cost_per_1k_rows_k": r["cost"] / r["n"] * 1000,
                         "saving_vs_flag_none_pct": 100 * (1 - r["cost"] / r["cost_none"])})
            if name == "data_driven":
                base_t[m] = r["t_star"]
    return pd.DataFrame(rows), base_t


# ----------------------------------------------------------------------------
# Final test evaluation (guarded: only once)
# ----------------------------------------------------------------------------
class TestGuard:
    used = False

    @classmethod
    def claim(cls) -> None:
        assert not cls.used, "TEST SET ALREADY EVALUATED - refusing a second evaluation"
        cls.used = True


def final_test(dev: pd.DataFrame, test: pd.DataFrame, best_params: dict[str, Any], t_star: dict[str, float],
               costs: dict[str, float]) -> tuple[pd.DataFrame, dict[str, np.ndarray], dict[str, Pipeline]]:
    TestGuard.claim()
    Xd, yd, Xt, yt = dev[FEATURES], dev["y"].to_numpy(), test[FEATURES], test["y"].to_numpy()
    fitted = {"LogReg_balanced": make_model(make_lr()).fit(Xd, yd),
              "LightGBM_tuned": make_model(make_lgbm(**best_params)).fit(Xd, yd)}
    rows, preds = [], {}
    for m, pipe in fitted.items():
        p = pipe.predict_proba(Xt)[:, 1]
        preds[m] = p
        fl = p >= t_star[m]
        tp, fp, fn = int((fl & (yt == 1)).sum()), int((fl & (yt == 0)).sum()), int((~fl & (yt == 1)).sum())
        cost = costs["c_fn_row_k"] * fn + costs["c_fp_k"] * fp
        none = costs["c_fn_row_k"] * yt.sum()
        rows.append({"model": m, "n_test": len(yt), "n_pos": int(yt.sum()), **metrics(yt, p),
                     "t_star_frozen": t_star[m], "flag_rate": fl.mean(), "recall_at_t": tp / max(yt.sum(), 1),
                     "precision_at_t": tp / max(tp + fp, 1), "TP": tp, "FP": fp, "FN": fn,
                     "exp_cost_k": cost, "saving_vs_flag_none_pct": 100 * (1 - cost / none)})
    return pd.DataFrame(rows), preds, fitted


# ----------------------------------------------------------------------------
# PySpark MLlib demo
# ----------------------------------------------------------------------------
def spark_mllib_demo(dev: pd.DataFrame, test: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    from part3_scalable_processing import make_spark
    from pyspark.ml import Pipeline as SPipeline
    from pyspark.ml.classification import LogisticRegression as SLR
    from pyspark.ml.feature import Imputer, StandardScaler as SScaler, VectorAssembler
    from pyspark.ml.functions import vector_to_array
    from pyspark.sql import functions as F

    PART5_DIR.mkdir(parents=True, exist_ok=True)
    wz = Winsorizer().fit(dev[FEATURES])                  # bounds from DEV only
    def prep(d: pd.DataFrame) -> pd.DataFrame:
        o = d[KEYS + ["y"]].copy()
        o[FEATURES] = wz.transform(d[FEATURES])
        return o
    prep(dev).to_parquet(PART5_DIR / "dev.parquet", index=False)
    prep(test).to_parquet(PART5_DIR / "test.parquet", index=False)
    spark = make_spark()
    rd = lambda n: spark.read.parquet(str(PART5_DIR / n).replace("\\", "/")).withColumn("y", F.col("y").cast("double"))  # noqa: E731
    sdev, stest = rd("dev.parquet"), rd("test.parquet")
    n, npos = sdev.count(), sdev.filter("y = 1").count()
    sdev = sdev.withColumn("w", F.when(F.col("y") == 1, n / (2.0 * npos)).otherwise(n / (2.0 * (n - npos))))
    imp = Imputer(inputCols=FEATURES, outputCols=[f"{f}_i" for f in FEATURES], strategy="median")
    asm = VectorAssembler(inputCols=[f"{f}_i" for f in FEATURES], outputCol="raw_features")
    sc = SScaler(inputCol="raw_features", outputCol="features", withMean=True, withStd=True)
    lr = SLR(featuresCol="features", labelCol="y", weightCol="w", maxIter=100, regParam=1e-4)
    t0 = time.time()
    model = SPipeline(stages=[imp, asm, sc, lr]).fit(sdev)
    fit_s = time.time() - t0
    pred = (model.transform(stest).select(*KEYS, "y", vector_to_array("probability")[1].alias("p")).toPandas())
    spark.stop()
    pred = pred.sort_values(KEYS).reset_index(drop=True)
    res = {"model": "Spark_MLlib_LogReg", "n_test": len(pred), "n_pos": int(pred["y"].sum()),
           **metrics(pred["y"].to_numpy(), pred["p"].to_numpy()), "fit_seconds": fit_s}
    return res, pred


# ----------------------------------------------------------------------------
# Feature importance + figures
# ----------------------------------------------------------------------------
def feature_importance(fitted: dict[str, Pipeline]) -> pd.DataFrame:
    rows = []
    for m, pipe in fitted.items():
        names = pipe.named_steps["prep"].get_feature_names_out()
        clf = pipe.named_steps["clf"]
        if m.startswith("LogReg"):
            imp = np.abs(clf.coef_[0])
            signed = clf.coef_[0]
        else:
            imp = clf.booster_.feature_importance("gain")
            imp = imp / imp.sum()
            signed = np.full(len(imp), np.nan)
        d = pd.DataFrame({"model": m, "feature": names, "importance": imp, "signed_coef": signed})
        d["rank"] = d["importance"].rank(ascending=False, method="first").astype(int)
        rows.append(d.sort_values("rank"))
    return pd.concat(rows, ignore_index=True)


def fig_curves(yt: np.ndarray, preds: dict[str, np.ndarray]) -> None:
    fig, ax = plt.subplots(1, 2, figsize=(12, 5))
    for m, p in preds.items():
        pr, rc, _ = precision_recall_curve(yt, p)
        fpr, tpr, _ = roc_curve(yt, p)
        ax[0].plot(rc, pr, label=f"{m} (AP={average_precision_score(yt, p):.3f})")
        ax[1].plot(fpr, tpr, label=f"{m} (AUC={roc_auc_score(yt, p):.3f})")
    ax[0].axhline(yt.mean(), color="gray", ls=":", label=f"prevalence={yt.mean():.4f}")
    ax[1].plot([0, 1], [0, 1], color="gray", ls=":")
    ax[0].set(xlabel="Recall", ylabel="Precision", title="Precision-Recall (Test 2008Q1-2025Q2)")
    ax[1].set(xlabel="False positive rate", ylabel="True positive rate", title="ROC (Test)")
    for a in ax:
        a.legend()
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(C.FIGURES / "fig4_pr_roc_curves.png", dpi=140)
    plt.close(fig)


def fig_cost(y: np.ndarray, oof: dict[str, np.ndarray], costs: dict[str, float], base_t: dict[str, float]) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    for m, p in oof.items():
        fr, cst = cost_curve(y, p, costs["c_fn_row_k"], costs["c_fp_k"])
        ax.plot(fr * 100, cst, label=m)
        r = best_threshold(y, p, costs["c_fn_row_k"], costs["c_fp_k"])
        ax.scatter([r["flag_rate"] * 100], [r["cost"] / r["n"] * 1000], s=60, zorder=5,
                   label=f"{m} t*={base_t[m]:.3f}")
    ax.set_xscale("log")
    ax.set(xlabel="% of bank-quarters flagged (log)", ylabel="expected cost per 1,000 bank-quarters ($k)",
           title=f"Expected cost on Dev OOF (C_FN/C_FP = {costs['base_ratio']:.0f})")
    ax.legend()
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(C.FIGURES / "fig5_cost_threshold_curve.png", dpi=140)
    plt.close(fig)


def fig_importance(imp: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    for ax, (m, col, title) in zip(axes, (("LightGBM_tuned", "importance", "LightGBM gain (normalised)"),
                                          ("LogReg_balanced", "importance", "LogReg |standardised coef|"))):
        d = imp[imp["model"] == m].nsmallest(20, "rank").iloc[::-1]
        ax.barh(d["feature"], d[col], color="#1f77b4" if m.startswith("Light") else "#e8710a")
        ax.set_title(title)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Top-20 features (final models fitted on Dev)")
    fig.tight_layout()
    fig.savefig(C.FIGURES / "fig6_feature_importance.png", dpi=140)
    plt.close(fig)


def fig_crisis(test: pd.DataFrame, p: np.ndarray, t: float) -> None:
    d = test[["REPDTE", "y"]].copy()
    d["p"], d["flag"] = p, p >= t
    d["date"] = pd.to_datetime(d["REPDTE"].astype(str), format="%Y%m%d")
    g = d.groupby("date").agg(pos=("y", "sum"), caught=("flag", lambda s: 0), n=("y", "size"),
                              mean_p=("p", "mean"), flagged=("flag", "mean"))
    g["caught"] = d[d["y"] == 1].groupby("date")["flag"].sum().reindex(g.index).fillna(0)
    fig, ax = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    ax[0].bar(g.index, g["pos"], width=70, color="firebrick", alpha=0.5, label="banks failing within 4Q (y=1)")
    ax[0].bar(g.index, g["caught"], width=70, color="navy", label="of which flagged at frozen t*")
    ax[0].set_ylabel("bank-quarters")
    ax[0].legend()
    ax[0].set_title("Test period crisis detection (LightGBM, frozen threshold)")
    ax[1].plot(g.index, g["mean_p"], color="purple", label="mean predicted risk")
    ax[1].plot(g.index, g["flagged"], color="darkorange", label="share of banks flagged")
    ax[1].set_yscale("log")
    ax[1].legend()
    for a in ax:
        a.axvspan(np.datetime64("2008-01-01"), np.datetime64("2012-12-31"), color="gray", alpha=0.12)
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(C.FIGURES / "fig7_test_crisis_detection.png", dpi=140)
    plt.close(fig)


# ----------------------------------------------------------------------------
def main() -> None:
    np.random.seed(C.SEED)
    for d in (C.TABLES, C.FIGURES, C.REPORTS):
        d.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    log("== Part 4: features + leakage tests ==")
    dev, test, raw = load_data()
    log(f"Dev rows={len(dev):,} (pos={int(dev['y'].sum()):,})  Test rows={len(test):,} (pos={int(test['y'].sum()):,})  "
        f"features={len(FEATURES)}")
    folds = make_folds(dev)
    rep = LeakageReport()
    leakage_tests_static(dev, test, raw, folds, rep)

    log("\n== Part 5: temporal CV (expanding window, 4Q embargo) ==")
    cv, oof, info = run_cv(dev, folds, rep)
    cv.to_csv(C.TABLES / "part5_cv_results.csv", index=False)
    leak = rep.frame()
    leak.to_csv(C.TABLES / "part5_leakage_checks.csv", index=False)
    log(f"best LightGBM: {info['best_name']} {info['best_params']}")

    log("\n== Cost threshold (Dev only) ==")
    y_oof = dev["y"].to_numpy()[info["val_idx"]]
    costs = estimate_costs(dev)
    log(json.dumps({k: round(v, 3) for k, v in costs.items()}))
    sens, base_t = threshold_sensitivity(y_oof, oof, costs)
    sens.to_csv(C.TABLES / "part5_cost_threshold_sensitivity.csv", index=False)
    (C.REPORTS / "part5_cost_parameters.json").write_text(json.dumps(costs, indent=1))

    log("\n== Final fit on Dev, ONE Test evaluation ==")
    test_res, preds, fitted = final_test(dev, test, info["best_params"], base_t, costs)
    imp = feature_importance(fitted)
    imp.to_csv(C.TABLES / "part5_feature_importance.csv", index=False)

    log("\n== Spark MLlib demo (Dev -> Test) ==")
    spark_res, spred = spark_mllib_demo(dev, test)
    key = test[KEYS + ["y"]].sort_values(KEYS).reset_index(drop=True)
    assert (key["CERT"].to_numpy() == spred["CERT"].to_numpy()).all() and \
        (key["REPDTE"].to_numpy() == spred["REPDTE"].to_numpy()).all(), "spark row alignment"
    test_all = pd.concat([test_res, pd.DataFrame([spark_res])], ignore_index=True)
    test_all.to_csv(C.TABLES / "part5_test_results.csv", index=False)

    # figures (reuse stored test predictions - no new evaluation)
    yt = test["y"].to_numpy()
    sp_aligned = spred.set_index(KEYS)["p"].reindex(pd.MultiIndex.from_frame(test[KEYS])).to_numpy()
    fig_curves(yt, {**preds, "Spark_MLlib_LogReg": sp_aligned})
    fig_cost(y_oof, oof, costs, base_t)
    fig_importance(imp)
    fig_crisis(test, preds["LightGBM_tuned"], base_t["LightGBM_tuned"])

    # ---- print
    pd.options.display.float_format = lambda v: f"{v:,.4f}"
    log("\n=== LEAKAGE CHECKS ===")
    log(leak[["check", "passed", "detail"]].to_string(index=False))
    log(f"ALL {len(leak)} LEAKAGE CHECKS PASSED: {bool(leak['passed'].all())}")
    log("\n=== CV (mean over folds + pooled OOF, selected models) ===")
    show = cv[cv["selected"] & cv["fold"].astype(str).isin(["mean_over_folds", "pooled_oof"])]
    log(show[["model", "fold", "n_val", "n_pos_val", "pr_auc", "roc_auc", "recall_top1", "recall_top5", "brier"]]
        .to_string(index=False))
    log("\n=== CV per fold (selected models) ===")
    log(cv[cv["selected"] & cv["fold"].apply(lambda v: isinstance(v, (int, np.integer)))][
        ["model", "fold", "val_period", "n_train", "n_val", "n_pos_val", "pr_auc", "roc_auc", "recall_top1"]]
        .to_string(index=False))
    log("\n=== TEST (single evaluation) ===")
    log(test_all.drop(columns=["fit_seconds"], errors="ignore").to_string(index=False))
    log("\n=== Cost threshold sensitivity (Dev OOF) ===")
    log(sens[["model", "scenario", "cfn_cfp_ratio", "t_star", "flag_rate", "recall", "precision",
              "saving_vs_flag_none_pct"]].to_string(index=False))
    log(f"\ntotal runtime {time.time() - t_start:.0f}s")
    log("Files:")
    for p_ in sorted(OUT.rglob("*")):
        if p_.is_file() and ("part5" in p_.name or p_.name.startswith(("fig4", "fig5", "fig6", "fig7"))):
            log(f"  {p_.relative_to(C.ROOT)}  ({p_.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
