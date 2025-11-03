"""Bundle :mod:`keirin_predictor` into a runnable archive.

Binary executables are not always suitable for the environments where this
project is used, so the default build artefact is now a Python zipapp
(``keirin_predictor.pyz``).  Zipapps can be executed with a standard Python 3
interpreter without requiring users to install the project as a package::

    $ python build_executable.py
    $ python dist/keirin_predictor.pyz --fetch

If a native executable is still desired you can explicitly request the
``pyinstaller`` format, provided the dependency is available::

    $ python build_executable.py --format pyinstaller
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile
import zipapp
from typing import List


def build(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create runnable bundles for keirin_predictor")
    parser.add_argument(
        "--format",
        choices=("zipapp", "pyinstaller", "auto"),
        default="zipapp",
        help=(
            "Bundle format to generate. 'zipapp' produces dist/keirin_predictor.pyz, "
            "'pyinstaller' emits a native executable when PyInstaller is installed, "
            "and 'auto' prefers PyInstaller but falls back to zipapp."
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Name of the generated artefact (defaults depend on the chosen format).",
    )

    args, remainder = parser.parse_known_args(argv)

    project_root = pathlib.Path(__file__).resolve().parent
    entry_script = project_root / "keirin_predictor.py"
    dist_dir = project_root / "dist"
    data_dir = project_root / "data"

    pyinstaller = None
    if args.format in {"pyinstaller", "auto"}:
        try:
            from PyInstaller import __main__ as pyinstaller  # type: ignore[assignment]
        except ModuleNotFoundError:
            if args.format == "pyinstaller":
                parser.error("PyInstaller is not installed; install it or choose --format zipapp")
            pyinstaller = None

    if args.format == "pyinstaller" or (args.format == "auto" and pyinstaller is not None):
        target_name = args.name or "keirin_predictor"
        return _build_pyinstaller(
            pyinstaller,
            remainder,
            target_name,
            dist_dir,
            entry_script,
            data_dir,
        )

    target_name = args.name or "keirin_predictor.pyz"
    return _build_zipapp(dist_dir, project_root, data_dir, target_name)


def _build_pyinstaller(pyinstaller_module, extra_args: List[str], target_name: str, dist_dir: pathlib.Path, entry_script: pathlib.Path, data_dir: pathlib.Path) -> int:
    """Invoke PyInstaller to create a native executable."""

    if pyinstaller_module is None:  # pragma: no cover - validated earlier
        raise RuntimeError("PyInstaller is not available")

    args = [
        "--name",
        target_name,
        "--onefile",
        "--console",
        f"--distpath={dist_dir}",
        str(entry_script),
    ]

    if data_dir.exists():
        args.extend(["--add-data", f"{data_dir}{os.pathsep}data"])

    if extra_args:
        args[4:4] = extra_args

    return pyinstaller_module.run(args) or 0


def _build_zipapp(
    dist_dir: pathlib.Path,
    project_root: pathlib.Path,
    data_dir: pathlib.Path,
    target_name: str,
) -> int:
    """Create a Python zipapp bundle."""

    dist_dir.mkdir(parents=True, exist_ok=True)
    target = dist_dir / target_name

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

    print(f"Created Python zipapp at {target}")
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

