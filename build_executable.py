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
import shutil
import sys
import tempfile
import zipapp
from typing import List


def build(argv: List[str] | None = None) -> int:
    project_root = pathlib.Path(__file__).resolve().parent
    entry_script = project_root / "keirin_predictor.py"
    dist_dir = project_root / "dist"
    data_dir = project_root / "data"

    try:
        from PyInstaller import __main__ as pyinstaller
    except ModuleNotFoundError:
        return _build_zipapp(dist_dir, project_root, data_dir)

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


def _build_zipapp(dist_dir: pathlib.Path, project_root: pathlib.Path, data_dir: pathlib.Path) -> int:
    """Fallback builder that creates a zipapp when PyInstaller is unavailable."""

    dist_dir.mkdir(parents=True, exist_ok=True)
    target = dist_dir / "keirin_predictor.exe"

    with tempfile.TemporaryDirectory() as tmpdir:
        staging = pathlib.Path(tmpdir)
        _copy_for_zipapp(staging, project_root, data_dir)
        # ``zipapp`` automatically marks the archive as executable on POSIX
        # platforms when ``interpreter`` is provided.
        zipapp.create_archive(
            staging,
            target,
            interpreter="/usr/bin/env python3",
        )

    print(
        "PyInstaller not available; created Python zipapp fallback at %s" % target
    )
    return 0


def _copy_for_zipapp(staging: pathlib.Path, project_root: pathlib.Path, data_dir: pathlib.Path) -> None:
    """Populate ``staging`` with the project files needed for the zipapp."""

    for filename in ("keirin_predictor.py", "keirin_fetcher.py", "keirin_models.py"):
        shutil.copy2(project_root / filename, staging / filename)

    if data_dir.exists():
        shutil.copytree(data_dir, staging / "data")

    launcher = staging / "__main__.py"
    launcher.write_text(
        "from keirin_predictor import main\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
        encoding="utf-8",
    )

if __name__ == "__main__":  # pragma: no cover - CLI entry point.
    raise SystemExit(build(sys.argv[1:]))

