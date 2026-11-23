# BFT65B – Bank Failure Early-Warning System (FDIC 1984Q1–2026Q2)

Reproducible Big-Data pipeline: acquisition → storage → data quality → scalable processing → leak-free features → temporal validation → cost-sensitive early warning.

## Repository structure

```
run_all.py                         # orchestrates Parts 1-5
requirements.txt                   # pinned dependencies
fdic_download_starter.py           # course starter (reference only)
src/
  config.py                        # paths, SEED=42, date boundaries, FDIC field lists
  part1_acquisition_storage.py     # resumable API downloader, manifest, Hive Parquet, storage benchmark
  part2_data_quality.py            # DQ audit, deterministic cleaning, point-in-time panel + labels
  part3_scalable_processing.py     # CAMELS workload on Pandas / Polars lazy / PySpark + equivalence check
  part4_5_modeling_pipeline.py     # features, temporal CV, LogReg / LightGBM / Spark MLlib, cost threshold
outputs/{tables,figures,reports,logs}   # all result artefacts (small, committed)
data_raw/ data_processed/ tools/        # DATA & local JDK - git-ignored, never committed
```

## Reproduction (Windows / PowerShell, Python 3.11)

```powershell
pip install -r requirements.txt
# Java 17 is required for PySpark (Parts 3 and 5). If `java` is not on PATH, put a JDK in tools/jdk/
# (the code auto-discovers it), e.g.:  pip install install-jdk ; python -c "import jdk; jdk.install('17', path='tools/jdk')"

python run_all.py --quick-test          # Part 1 on 4 quarters (~1 min) - smoke test
python run_all.py                       # full pipeline; Part 1 download takes ~1 hour (resumable)
python run_all.py --skip-download       # reuse data_raw/
python run_all.py --from-part 3         # resume later stages
python src/part4_5_modeling_pipeline.py # a single stage
```
Spark needs a local socket: if you use a sandboxed shell, run Parts 3 and 4/5 unsandboxed.
Seeds: `SEED = 42` (config.py) for LightGBM, hyper-parameter sampling and NumPy.

## Design decisions (short)

| Topic | Decision |
|---|---|
| Splits | Dev 1984Q1–2006Q4 · embargo 2007Q1–Q4 (dropped) · Test 2008Q1–2025Q2 (evaluated once) · 2025Q3+ unobservable label window, excluded |
| Target | `y=1` iff `REPDTE < FAILDATE <= REPDTE + 4Q`; `ASSISTANCE` rows are not failures; mergers/voluntary exits = 0 |
| Exclusions | post-failure reporting rows (`FLAG_POST_FAILURE_ROW`, 1,211) and `REPDTE > 2025-06-30` |
| Missing data | structural NaNs preserved; pre-1990 `RBCRWAJ/RBC1AAJ` zeros set to NaN (calendar rule); imputation only inside the CV pipeline |
| Features | 32 backward-looking CAMELS features (Polars, gap-safe quarter index); peer z-scores within each `REPDTE` only |
| Preprocessing | `Winsorizer(0.5/99.5%)` → `SimpleImputer(median, add_indicator)` → `StandardScaler`, fitted on training rows of each fold |
| CV | expanding window, 5 folds (val 1990–92, 93–95, 96–98, 99–02, 03–06), 4-quarter embargo before each validation block |
| Models | LogisticRegression(balanced), LightGBM (6-config random search on CV PR-AUC), Spark MLlib LogReg demo |
| Cost rule | `C_FN` = mean FDIC `COST` of Dev failures (n=2,288) ÷ 3.8 positive quarters per failure; `C_FP` = $100k per flagged bank-quarter (**assumption**); `t*` minimises expected cost on Dev OOF predictions; sensitivity over `C_FN/C_FP` ∈ {10…1000} |

## Leakage controls (automated: `outputs/tables/part5_leakage_checks.csv`, 41 checks)
* no `META_*`, `LABEL_*`, `FLAG_*` column reaches `X`; feature builder never receives them;
* `max(dev_date) + 4Q < min(test_date)`; dev/test disjoint; no embargo rows anywhere;
* every CV fold: ≥4Q embargo, train label horizon ends before validation starts, preprocessing refit on that fold's training rows (row count and quantile bounds re-verified);
* features of rows ≤ 2000-12-31 are bit-identical when all later data is deleted (backward-looking proof);
* a `TestGuard` raises if the Test set is evaluated a second time.

## Key results (Test 2008Q1–2025Q2; 435,446 bank-quarters, 2,134 positives)

| Model | PR-AUC | ROC-AUC | Recall@1% | Recall@5% | Brier |
|---|---|---|---|---|---|
| LogReg (balanced) | 0.453 | 0.986 | 0.751 | 0.949 | 0.0087 |
| LightGBM (tuned) | **0.612** | 0.982 | 0.780 | 0.941 | **0.0031** |
| Spark MLlib LogReg | 0.533 | 0.985 | 0.759 | 0.946 | 0.0128 |

Frozen `t*` (Dev OOF): LightGBM flags 2.8% of bank-quarters and catches 91.8% of positives (88.5% cost saving vs. flagging nothing). CV and sensitivity tables: `outputs/tables/part5_*.csv`. Dev CV pooled-OOF PR-AUC: LightGBM 0.386, LogReg 0.221.

## Academic integrity, AI use and data citations
* **Data**: Federal Deposit Insurance Corporation, *BankFind Suite API* (`/financials`, `/failures`, `/institutions`), https://api.fdic.gov/banks/docs/, accessed via the course starter `fdic_download_starter.py`. Raw and processed data are not redistributed (git-ignored).
* **AI assistance** (via Google Antigravity, under the student's specification; all output executed and verified by the student):
  * **Claude Sonnet** – code implementation and debugging (`src/`, `run_all.py`, `README.md`).
  * **Gemini 4 Neon** – rubric auditing and report-drafting assistance.

  The student is responsible for understanding, validating and documenting every design choice in the final report.
* **Repository**: https://github.com/idonkno/BFT65B_DoHoangQuan_
* **Data download date**: 2026-11-23
* **Libraries**: pandas, NumPy, PyArrow, Polars, PySpark, scikit-learn, LightGBM, Matplotlib, Seaborn, psutil (versions pinned in `requirements.txt`).
* **Declaration**: *I confirm that this submission is my own work, that all sources and AI tools are acknowledged above, and that no test-period information was used for model selection.* — Name: Do Hoang Quan · Student ID: 11231008 · Date: 2026-11-23
