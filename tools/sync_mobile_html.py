"""Regenerate the multi-map Android pages from the root HTML template.

The old single-file copy is gone.  Kept as a compatibility entry point because
existing notes say "run tools/sync_mobile_html.py".  Prefer tools/build_maps.py.
"""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools" / "build_maps.py"

if __name__ == "__main__":
    subprocess.run([sys.executable, str(BUILDER)], check=True)
