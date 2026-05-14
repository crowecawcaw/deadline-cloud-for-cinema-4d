# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
End-to-end test orchestrator for the Cinema 4D Deadline Cloud submitter.

Runs as the OpenJD Task action on a Deadline Cloud worker. The job template
only handles environment setup (cinema4d conda package, paths); this script
contains the test logic.

Two source modes are supported:

  * ``github`` — clones (downloads zip of) the submitter repo at a given ref
    and pip-installs it into Cinema 4D's bundled Python. Intended for quick
    development iteration.

  * ``installer`` — runs an InstallBuilder-produced installer .exe attached
    to the job in unattended mode. Intended for the full integration test:
    exercises the same artifact end users get.

Steps performed (both modes):
  1. Resolve C4D paths from environment.
  2. Stage the submitter at <work>/submitter-install (mode-dependent).
  3. Run driver.py inside c4dpy with C4DPYTHONPATH311 pointing at the install.
  4. Inspect the exported bundle and assert on its contents.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[run_test] {msg}", flush=True)


def section(title: str) -> None:
    print(flush=True)
    print("=" * 60, flush=True)
    print(title, flush=True)
    print("=" * 60, flush=True)


def _build_ssl_context() -> ssl.SSLContext:
    """
    Cinema 4D's bundled Python on Windows ships without a CA bundle, so
    ``urllib`` HTTPS requests fail with ``CERTIFICATE_VERIFY_FAILED``. Try
    in order:

        1. certifi (if importable) — most portable.
        2. Windows certificate store via ssl.enum_certificates.
        3. Last-resort: an unverified context with a clear warning.
    """
    try:
        import certifi  # type: ignore[import-not-found]
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if sys.platform == "win32":
        try:
            ctx = ssl.create_default_context()
            ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
            for store in ("ROOT", "CA"):
                for cert, _, _ in ssl.enum_certificates(store):
                    try:
                        ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
                    except ssl.SSLError:
                        continue
            return ctx
        except Exception:
            pass
    log("WARNING: falling back to an unverified SSL context for HTTPS download")
    return ssl._create_unverified_context()


# --------------------------------------------------------------------------- #
# C4D environment resolution
# --------------------------------------------------------------------------- #
def resolve_c4d_paths() -> dict[str, Path]:
    """Locate c4dpy.exe, the bundled python, etc., from the activated env."""
    c4d_location = os.environ.get("C4D_LOCATION")
    if not c4d_location:
        raise RuntimeError(
            "C4D_LOCATION is not set. The cinema4d conda package was not activated."
        )
    c4d = Path(c4d_location)
    if sys.platform == "win32":
        paths = {
            "c4d_location": c4d,
            "c4dpy": c4d / "c4dpy.exe",
            "commandline": c4d / "Commandline.exe",
            "python": c4d / "resource" / "modules" / "python" / "libs" / "win64" / "python.exe",
        }
    else:
        paths = {
            "c4d_location": c4d,
            "c4dpy": c4d / "c4dpy",
            "commandline": c4d / "bin" / "Commandline",
            "python": c4d / "resource" / "modules" / "python" / "libs" / "linux64" / "python",
        }
    for name, path in paths.items():
        log(f"{name:14}: {path} (exists={path.exists()})")
    if not paths["c4dpy"].exists():
        raise RuntimeError(f"c4dpy not found at {paths['c4dpy']}")
    return paths


# --------------------------------------------------------------------------- #
# Source mode: github
# --------------------------------------------------------------------------- #
def install_test_deps(c4d_python: Path) -> None:
    """
    Pre-install the python deps the test itself needs, into Cinema 4D's
    bundled Python:
        * certifi — for HTTPS downloads (C4D's python ships without a CA bundle)
        * pyyaml  — for the bundle assertions
        * hatchling / hatch-vcs — used by the submitter build (github mode)
        * pip upgrade — newer pip handles --no-build-isolation cleanly
    """
    log(f"Pre-installing test deps via {c4d_python}")
    subprocess.run(
        [str(c4d_python), "-m", "pip", "install", "--no-warn-script-location",
         "--upgrade", "pip", "hatchling", "hatch-vcs", "pyyaml", "certifi"],
        check=True,
    )


def stage_from_github(work_dir: Path, repo: str, ref: str, c4d_python: Path) -> Path:
    """
    Download the submitter source as a GitHub archive and pip-install it into
    a target dir. Returns the install dir (suitable for C4DPYTHONPATH311).
    """
    section(f"Staging submitter from GitHub: {repo}@{ref}")

    # Normalize repo to "owner/name" form
    repo_path = repo.replace(".git", "")
    repo_path = repo_path.split("github.com/", 1)[-1]

    src_zip = work_dir / "src.zip"
    src_extract = work_dir / "src-extract"
    submitter_src = work_dir / "submitter-src"
    install_dir = work_dir / "submitter-install"

    for p in (src_extract, submitter_src, install_dir):
        if p.exists():
            shutil.rmtree(p)
    src_extract.mkdir()
    install_dir.mkdir()

    zip_url = f"https://github.com/{repo_path}/archive/{ref}.zip"
    log(f"Downloading {zip_url}")
    # Cinema 4D's bundled Python doesn't ship a CA bundle, so urlretrieve's
    # default SSL context fails verification on Windows. Use a context backed
    # by certifi if available; otherwise fall back to the system store via
    # ssl.create_default_context() with a Windows-specific cert load.
    ctx = _build_ssl_context()
    with urllib.request.urlopen(zip_url, context=ctx) as resp, open(src_zip, "wb") as f:
        shutil.copyfileobj(resp, f)

    log(f"Extracting to {src_extract}")
    with zipfile.ZipFile(src_zip) as zf:
        zf.extractall(src_extract)

    # GitHub archives extract to a single top-level dir.
    children = [c for c in src_extract.iterdir() if c.is_dir()]
    if len(children) != 1:
        raise RuntimeError(f"Unexpected extracted layout: {children}")
    children[0].rename(submitter_src)

    log(f"pip install '{submitter_src}'[gui] -> {install_dir}")
    # The hatch-vcs version source needs git history that GitHub source-zips
    # don't carry. Pretend a version so the build can succeed.
    env = {
        **os.environ,
        "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0+e2e",
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DEADLINE_CLOUD_FOR_CINEMA_4D": "0.0.0+e2e",
        "HATCH_BUILD_HOOK_VCS_VERSION": "0.0.0+e2e",
    }

    # Build deps already installed up front. Now install the submitter.
    subprocess.run(
        [str(c4d_python), "-m", "pip", "install", "--no-warn-script-location",
         "--no-build-isolation", "--target", str(install_dir),
         f"{submitter_src}[gui]"],
        check=True, env=env,
    )
    return install_dir


# --------------------------------------------------------------------------- #
# Source mode: installer
# --------------------------------------------------------------------------- #
def stage_from_installer(work_dir: Path, installer_path: Path) -> Path:
    """
    Run the InstallBuilder-produced installer in unattended mode and return
    the install dir (which the installer also exposes as C4DPYTHONPATH311).
    """
    section(f"Staging submitter from installer: {installer_path}")
    if not installer_path.is_file():
        raise FileNotFoundError(f"Installer not found at {installer_path}")

    install_dir = work_dir / "submitter-install"
    if install_dir.exists():
        shutil.rmtree(install_dir)

    if sys.platform == "win32":
        # InstallBuilder's unattended mode flags. --prefix sets the install dir.
        cmd = [
            str(installer_path),
            "--mode", "unattended",
            "--unattendedmodeui", "none",
            "--prefix", str(install_dir),
        ]
    else:
        # On macOS/Linux the installer is a different format; not supported here yet.
        raise RuntimeError(
            f"Installer mode is only supported on Windows for now (sys.platform={sys.platform})"
        )

    log(f"Running installer: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    log(f"Installer stdout:\n{result.stdout}")
    if result.stderr:
        log(f"Installer stderr:\n{result.stderr}")
    if result.returncode != 0:
        raise RuntimeError(f"Installer failed with exit code {result.returncode}")

    if not install_dir.is_dir():
        raise RuntimeError(f"Installer did not create expected directory {install_dir}")
    log(f"Install contents: {sorted(p.name for p in install_dir.iterdir())}")
    return install_dir


# --------------------------------------------------------------------------- #
# Driver step (runs inside c4dpy)
# --------------------------------------------------------------------------- #
def run_driver(c4dpy: Path, driver: Path, submitter_install: Path, bundle_dir: Path) -> None:
    section("Running driver inside c4dpy")
    bundle_dir.mkdir(parents=True, exist_ok=True)
    if not driver.is_file():
        raise FileNotFoundError(f"driver.py not found at {driver}")

    # c4dpy can be picky about absolute paths in argv[0]; cd to the driver dir
    # and pass the basename.
    env = {
        **os.environ,
        # Prepend the install dir so the submitter is importable inside c4dpy.
        # C4DPYTHONPATH311 is C4D's plugin search path; PYTHONPATH covers
        # ordinary import resolution inside c4dpy too.
        "C4DPYTHONPATH311": (
            str(submitter_install) + os.pathsep + os.environ.get("C4DPYTHONPATH311", "")
        ),
        "PYTHONPATH": (
            str(submitter_install) + os.pathsep + os.environ.get("PYTHONPATH", "")
        ),
    }

    log(f"c4dpy: {c4dpy}")
    log(f"driver: {driver}")
    log(f"bundle out: {bundle_dir}")
    result = subprocess.run(
        [str(c4dpy), driver.name, str(bundle_dir)],
        cwd=str(driver.parent),
        env=env,
    )
    # c4dpy frequently exits non-zero on shutdown after a successful script.
    # The bundle assertions below are the source of truth.
    log(f"c4dpy exit code: {result.returncode} (will validate via bundle assertions)")


# --------------------------------------------------------------------------- #
# Bundle assertions
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> Any:
    import yaml  # type: ignore[import-not-found]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _check(cond: bool, msg: str) -> None:
    if not cond:
        log(f"FAIL: {msg}")
        raise AssertionError(msg)
    log(f"OK:   {msg}")


def assert_bundle(bundle_dir: Path) -> None:
    section("Asserting on exported bundle")
    log(f"bundle dir: {bundle_dir}")
    log(f"contents:   {sorted(p.name for p in bundle_dir.iterdir())}")

    template_path = bundle_dir / "template.yaml"
    params_path = bundle_dir / "parameter_values.yaml"
    refs_path = bundle_dir / "asset_references.yaml"

    _check(template_path.exists(), "template.yaml exists")
    _check(params_path.exists(), "parameter_values.yaml exists")
    _check(refs_path.exists(), "asset_references.yaml exists")

    template = _load_yaml(template_path)
    params = _load_yaml(params_path)
    refs = _load_yaml(refs_path)

    _check(
        template.get("specificationVersion") == "jobtemplate-2023-09",
        "template specificationVersion is jobtemplate-2023-09",
    )
    _check(isinstance(template.get("name"), str) and template["name"], "template has a name")
    _check(
        isinstance(template.get("steps"), list) and len(template["steps"]) >= 1,
        "template has at least one step",
    )
    _check(
        any(p.get("name") == "Cinema4DFile" for p in template.get("parameterDefinitions", [])),
        "template defines a Cinema4DFile parameter",
    )
    _check(
        any(p.get("name") == "Frames" for p in template.get("parameterDefinitions", [])),
        "template defines a Frames parameter",
    )

    pv = {p["name"]: p["value"] for p in params.get("parameterValues", [])}
    _check("Cinema4DFile" in pv, "parameter_values has Cinema4DFile")
    _check(
        isinstance(pv.get("Cinema4DFile"), str) and pv["Cinema4DFile"].endswith(".c4d"),
        f"Cinema4DFile points at a .c4d file (got {pv.get('Cinema4DFile')!r})",
    )
    _check("Frames" in pv, "parameter_values has Frames")

    asset_refs = refs.get("assetReferences", {})
    inputs = asset_refs.get("inputs", {}) or {}
    outputs = asset_refs.get("outputs", {}) or {}
    input_filenames = inputs.get("filenames", []) or []
    output_dirs = outputs.get("directories", []) or []
    _check(
        any(str(f).endswith(".c4d") for f in input_filenames),
        f"asset_references.inputs.filenames includes a .c4d (got {input_filenames!r})",
    )
    _check(
        len(output_dirs) >= 1,
        f"asset_references.outputs.directories has at least one entry (got {output_dirs!r})",
    )

    log("")
    log("template summary:")
    log(f"  name:        {template.get('name')}")
    log(f"  steps:       {[s.get('name') for s in template.get('steps', [])]}")
    log(f"  param count: {len(template.get('parameterDefinitions', []))}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["github", "installer"], required=True,
        help="Where to get the submitter from."
    )
    parser.add_argument(
        "--work-dir", required=True, type=Path,
        help="Workspace directory for staging files (typically the session working dir)."
    )
    parser.add_argument(
        "--git-repo", default="https://github.com/aws-deadline/deadline-cloud-for-cinema-4d.git",
        help="GitHub repo URL (mode=github)."
    )
    parser.add_argument(
        "--git-ref", default="mainline",
        help="Git branch, tag, or commit (mode=github)."
    )
    parser.add_argument(
        "--installer-path", type=Path,
        help="Path to the InstallBuilder-produced installer .exe (mode=installer)."
    )
    parser.add_argument(
        "--driver", type=Path,
        help="Path to driver.py. Defaults to <work-dir>/driver.py if it exists, else "
             "the driver.py next to this file."
    )
    args = parser.parse_args()

    section("C4D Submitter E2E Test")
    log(f"mode:           {args.mode}")
    log(f"work dir:       {args.work_dir}")
    log(f"sys.platform:   {sys.platform}")
    log(f"sys.executable: {sys.executable}")

    paths = resolve_c4d_paths()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    install_test_deps(paths["python"])

    if args.mode == "github":
        install_dir = stage_from_github(
            args.work_dir, args.git_repo, args.git_ref, paths["python"]
        )
    else:  # installer
        if args.installer_path is None:
            parser.error("--installer-path is required when --mode=installer")
        install_dir = stage_from_installer(args.work_dir, args.installer_path)

    log(f"submitter install dir: {install_dir}")
    log(
        f"contents (top 10): "
        f"{sorted(p.name for p in install_dir.iterdir())[:10]}"
    )

    # Resolve driver.py
    if args.driver:
        driver = args.driver
    elif (args.work_dir / "driver.py").is_file():
        driver = args.work_dir / "driver.py"
    else:
        driver = Path(__file__).parent / "driver.py"

    bundle_dir = args.work_dir / "exported-bundle"
    run_driver(paths["c4dpy"], driver, install_dir, bundle_dir)

    assert_bundle(bundle_dir)

    section("C4D Submitter E2E Test PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[run_test] ERROR: {e}", file=sys.stderr, flush=True)
        import traceback

        traceback.print_exc()
        sys.exit(1)
