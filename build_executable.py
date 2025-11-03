"""Helper script to bundle :mod:`keirin_predictor` into a standalone executable.

The script is intentionally lightweight and delegates the heavy lifting to
`PyInstaller <https://pyinstaller.org/>`.  Run it in an environment where
PyInstaller is available to produce a console application named
``keirin_predictor.exe`` (regardless of host platform).  Example::

    $ python -m pip install pyinstaller
    $ python build_executable.py

The resulting binary is emitted into ``dist/keirin_predictor.exe``.  On
non-Windows hosts the ``.exe`` suffix is preserved for convenience, but the
binary will target the local operating system.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import List


def build(argv: List[str] | None = None) -> int:
    try:
        from PyInstaller import __main__ as pyinstaller
    except ModuleNotFoundError as exc:  # pragma: no cover - requires missing dependency.
        raise SystemExit(
            "PyInstaller is required. Install it with 'python -m pip install pyinstaller'."
        ) from exc

    project_root = pathlib.Path(__file__).resolve().parent
    entry_script = project_root / "keirin_predictor.py"
    dist_dir = project_root / "dist"
    data_dir = project_root / "data"

    # Use a Windows-style suffix for discoverability, even on POSIX systems.
    args = [
        "--name",
        "keirin_predictor.exe",
        "--onefile",
        "--console",
        f"--distpath={dist_dir}",
        str(entry_script),
    ]

    if data_dir.exists():
        args.extend(["--add-data", f"{data_dir}{os.pathsep}data"])

    if argv:
        args[4:4] = argv  # allow callers to inject extra PyInstaller options

    return pyinstaller.run(args) or 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point.
    raise SystemExit(build(sys.argv[1:]))

