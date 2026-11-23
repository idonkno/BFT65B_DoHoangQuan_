"""Orchestrate the full BFT65B pipeline (Parts 1-5).

    python run_all.py                       # everything (Part 1 downloads ~1h)
    python run_all.py --skip-download       # reuse data_raw/, rebuild Parquet + benchmark onwards
    python run_all.py --quick-test          # Part 1 on 4 quarters only, then stop (later parts need full data)
    python run_all.py --from-part 3         # resume from Part 3
    python run_all.py --only 2 4            # run selected parts (Part 4 = Parts 4+5 script)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
STEPS: dict[int, tuple[str, str]] = {
    1: ("Part 1 - acquisition & storage", "part1_acquisition_storage.py"),
    2: ("Part 2 - data quality & point-in-time panel", "part2_data_quality.py"),
    3: ("Part 3 - scalable processing & engine benchmarks", "part3_scalable_processing.py"),
    4: ("Parts 4+5 - features, modelling, validation", "part4_5_modeling_pipeline.py"),
}


def run(script: str, extra: list[str]) -> None:
    cmd = [sys.executable, str(SRC / script), *extra]
    print(f"\n$ {' '.join(cmd)}", flush=True)
    t0 = time.time()
    subprocess.run(cmd, check=True, cwd=ROOT)
    print(f"-> done in {time.time() - t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-download", action="store_true", help="Part 1: do not call the FDIC API")
    ap.add_argument("--quick-test", action="store_true", help="Part 1 quick test (4 quarters) and stop")
    ap.add_argument("--from-part", type=int, default=1, choices=STEPS)
    ap.add_argument("--only", type=int, nargs="+", choices=STEPS)
    a = ap.parse_args()

    if a.quick_test:
        run(STEPS[1][1], ["--quick-test"])
        return
    parts = a.only or [p for p in STEPS if p >= a.from_part]
    for p in parts:
        name, script = STEPS[p]
        print(f"\n{'=' * 78}\n{name}\n{'=' * 78}", flush=True)
        run(script, ["--skip-download"] if (p == 1 and a.skip_download) else [])
    print("\nAll requested parts finished. Outputs in outputs/.")


if __name__ == "__main__":
    main()
