"""One-command setup for FounderSearch on any machine:   python setup.py

Creates .venv, installs dependencies, creates .env from the template, then (once .env is filled in) runs the
connection check. Safe to run again at any time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*cmd: str) -> int:
    return subprocess.call(list(cmd), cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})


def main() -> int:
    if sys.version_info < (3, 10):
        print(f"Python 3.10+ is required (found {sys.version.split()[0]}). Install it from python.org and retry.")
        return 1

    if not PY.exists():
        print("1/4  Creating virtual environment (.venv) ...")
        venv.create(VENV, with_pip=True)
    else:
        print("1/4  Virtual environment already exists.")

    print("2/4  Installing dependencies ...")
    if run(str(PY), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "requirements.txt"):
        print("Dependency install failed. Check your internet connection and run `python setup.py` again.")
        return 1

    env = ROOT / ".env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
        print("3/4  Created .env from the template.")
    else:
        print("3/4  .env already exists (left untouched).")

    text = env.read_text(encoding="utf-8")
    filled = all(f"{k}=" in text and not _empty(text, k) for k in ("IMAP_USER", "IMAP_PASSWORD"))
    if not filled:
        print("\nNEXT: open .env and fill in IMAP_USER and IMAP_PASSWORD (Gmail app password) and, for the AI filter,")
        print("      LLM_API_KEY and LLM_MODEL. See README.md, section 'Accounts and keys'. Then run:")
        print("          python setup.py        (verifies every connection)")
        return 0

    print("4/4  Checking connections ...\n")
    code = run(str(PY), "-m", "foundersearch", "doctor")
    if code == 0:
        print("\nAll good. Start it with:   python run.py     then open http://127.0.0.1:8765")
    return code


def _empty(text: str, key: str) -> bool:
    for line in text.splitlines():
        if line.startswith(key + "="):
            return not line.split("=", 1)[1].split("#", 1)[0].strip()
    return True


if __name__ == "__main__":
    sys.exit(main())
