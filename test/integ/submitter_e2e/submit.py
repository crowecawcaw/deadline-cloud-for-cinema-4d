#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Submission helper for the C4D submitter E2E test.

Embeds run_test.py and driver.py into a copy of the bundle's template.yaml
(so they ride along as OpenJD embedded files) and shells out to
``deadline bundle submit``.

Usage:
    # Default github mode against the upstream repo
    python submit.py

    # github mode, custom ref / fork
    python submit.py --git-ref my-branch \\
        --git-repo https://github.com/me/deadline-cloud-for-cinema-4d.git

    # installer mode, attaching a locally-built installer
    python submit.py --mode installer \\
        --installer ../../../DeadlineCloudForCinema4DSubmitter-windows-x64-installer.exe

Any extra args (e.g. ``--queue-id``) are forwarded to ``deadline bundle submit``.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE_DIR = HERE / "job"
RUN_TEST = HERE / "run_test.py"
DRIVER = HERE / "driver.py"


def _embed(template_text: str, name: str, source: Path) -> str:
    """Replace the placeholder body of an embeddedFile with the given source."""
    body = textwrap.indent(source.read_text(encoding="utf-8"), "        ")
    # Locate the embedded-file block: anchored on `name: <name>` then `data: |`.
    marker = f"- name: {name}\n"
    idx = template_text.find(marker)
    if idx < 0:
        raise RuntimeError(f"Could not find embedded file block for {name}")
    data_marker = "data: |\n"
    data_idx = template_text.find(data_marker, idx)
    if data_idx < 0:
        raise RuntimeError(f"Could not find data block for {name}")
    body_start = data_idx + len(data_marker)
    # Body extends until the next line that is dedented (no leading 8 spaces).
    end = body_start
    for line in template_text[body_start:].splitlines(keepends=True):
        if line.strip() and not line.startswith("        "):
            break
        end += len(line)
    return template_text[:body_start] + body + "\n" + template_text[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["github", "installer"], default="github")
    parser.add_argument("--git-repo")
    parser.add_argument("--git-ref")
    parser.add_argument("--installer", type=Path)
    parser.add_argument(
        "--keep", action="store_true",
        help="Don't delete the staged bundle dir on exit (useful for debugging)."
    )
    args, deadline_args = parser.parse_known_args()

    template = (BUNDLE_DIR / "template.yaml").read_text(encoding="utf-8")
    template = _embed(template, "RunTest", RUN_TEST)
    template = _embed(template, "Driver", DRIVER)

    staged = Path(tempfile.mkdtemp(prefix="c4d-e2e-"))
    try:
        # Copy job/* into staged, replacing template.yaml with the embedded one.
        for src in BUNDLE_DIR.iterdir():
            if src.name == "template.yaml":
                continue
            shutil.copy2(src, staged / src.name)
        (staged / "template.yaml").write_text(template, encoding="utf-8")

        cmd = [
            "deadline", "bundle", "submit", str(staged),
            "-p", f"SourceMode={args.mode}",
        ]
        if args.git_repo:
            cmd.extend(["-p", f"GitRepo={args.git_repo}"])
        if args.git_ref:
            cmd.extend(["-p", f"GitRef={args.git_ref}"])
        if args.mode == "installer":
            if not args.installer:
                parser.error("--installer is required when --mode=installer")
            installer_abs = args.installer.resolve()
            if not installer_abs.is_file():
                parser.error(f"installer not found: {installer_abs}")
            cmd.extend(["-p", f"Installer={installer_abs}"])
        cmd.extend(deadline_args)

        print(f"Running: {' '.join(cmd)}")
        return subprocess.call(cmd)
    finally:
        if not args.keep:
            shutil.rmtree(staged, ignore_errors=True)
        else:
            print(f"Kept staged bundle at: {staged}")


if __name__ == "__main__":
    sys.exit(main())
