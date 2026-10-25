"""Start FounderSearch using the project's virtual environment:   python run.py   (extra args are passed on,
e.g. `python run.py fetch`, `python run.py doctor`)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

if not PY.exists():
    sys.exit("Not set up yet. Run:  python setup.py")

try:
    sys.exit(subprocess.call([str(PY), "-m", "foundersearch", *(sys.argv[1:] or ["run"])], cwd=ROOT,
                             env={**os.environ, "PYTHONPATH": str(ROOT)}))
except KeyboardInterrupt:
    sys.exit(0)
