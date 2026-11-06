"""Run the local batch CLI and its workers from the same corrected checkout."""

import os
import runpy
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "src"
    if not (source / "lexradar" / "batch_pipeline.py").is_file():
        # The historical workspace keeps the current implementation in its own checkout.
        source = root / "runs-122" / "implementation" / "src"
    if not (source / "lexradar" / "batch_pipeline.py").is_file():
        raise SystemExit("Corrected batch checkout not found; see runs-122/START-HERE.md")
    sys.path.insert(0, str(source))
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(source), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    )
    runpy.run_module("lexradar.batch_pipeline", run_name="__main__")


if __name__ == "__main__":
    main()
