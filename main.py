"""
Backwards-compatible runner: `python main.py DEMO-2 --publish gdocs`.

Prefer the installed `docgen` command (pip install -e .). This shim lets the
old invocation keep working from a checkout without installing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docgen.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
