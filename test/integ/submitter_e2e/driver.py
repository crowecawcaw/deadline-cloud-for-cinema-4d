# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
c4dpy-side driver for the E2E test.

Runs inside Cinema 4D's bundled Python (c4dpy). Builds a tiny scene, launches
the real Qt submitter dialog (the same one users see when they click
"Submit to AWS Deadline Cloud"), and lets a sidecar process (automate.py,
running in a regular Python interpreter) drive it via xa11y to:

  * screenshot the submitter window
  * click the "Export bundle" button
  * dismiss the success message dialog

The sidecar approach exists because xa11y's Windows UI Automation backend
can't reliably introspect the calling process from a worker thread while
the main thread is parked inside QDialog.exec_(). Driving the UI from a
separate process side-steps that entirely.

Usage (called by run_test.py):
    c4dpy.exe driver.py <output-dir>
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import c4d  # type: ignore[import-not-found]


# os.startfile() opens the OS file explorer on the bundle dir after a
# successful Export bundle. On a Deadline Cloud worker that's noise at best
# and can fail at worst, so neutralize it before the dialog imports run.
if hasattr(os, "startfile"):
    os.startfile = lambda *args, **kwargs: None  # type: ignore[assignment]


def log(msg: str) -> None:
    print(f"[driver] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Scene setup
# --------------------------------------------------------------------------- #
def _build_scene(scene_dir: Path) -> Path:
    """Create a minimal scene file the submitter can dump."""
    doc = c4d.documents.GetActiveDocument()
    doc.Flush()

    cube = c4d.BaseObject(c4d.Ocube)
    cube[c4d.PRIM_CUBE_LEN] = c4d.Vector(200, 200, 200)
    cube.SetAbsPos(c4d.Vector(0, 100, 0))
    doc.InsertObject(cube)

    render_data = doc.GetActiveRenderData()
    render_data[c4d.RDATA_PATH] = "renders/$prj"
    render_data[c4d.RDATA_FRAMEFROM] = c4d.BaseTime(1, doc.GetFps())
    render_data[c4d.RDATA_FRAMETO] = c4d.BaseTime(1, doc.GetFps())
    render_data[c4d.RDATA_RENDERENGINE] = 1023342  # Standard / Physical
    render_data[c4d.RDATA_FORMAT] = c4d.FILTER_PNG
    render_data[c4d.RDATA_MULTIPASS_SAVEIMAGE] = False

    scene_dir.mkdir(parents=True, exist_ok=True)
    scene_path = scene_dir / "e2e_test.c4d"
    doc.SetDocumentPath(str(scene_dir))
    doc.SetDocumentName(scene_path.name)
    c4d.documents.SaveDocument(
        doc, str(scene_path), c4d.SAVEDOCUMENTFLAGS_0, c4d.FORMAT_C4DEXPORT
    )
    c4d.EventAdd()
    return scene_path


# --------------------------------------------------------------------------- #
# Sidecar discovery
# --------------------------------------------------------------------------- #
def _find_sidecar_python() -> Path:
    """
    The sidecar must run outside c4dpy. xa11y was installed via
    install_test_deps() into Cinema 4D's bundled python interpreter
    (``<C4D>/resource/modules/python/libs/<arch>/python.exe``), so use
    *that* python — it sees the same site-packages where xa11y lives.
    """
    c4d_location = os.environ.get("C4D_LOCATION")
    candidates: list[Path] = []
    if c4d_location:
        c4d = Path(c4d_location)
        if sys.platform == "win32":
            candidates.append(c4d / "resource" / "modules" / "python" / "libs" / "win64" / "python.exe")
        else:
            candidates.append(c4d / "resource" / "modules" / "python" / "libs" / "linux64" / "python")

    for p in candidates:
        if p.is_file():
            return p

    # Last resort: rely on PATH.
    fallback = shutil.which("python") or shutil.which("python.exe")
    if not fallback:
        raise RuntimeError(
            "No usable Python interpreter found for the xa11y sidecar."
        )
    return Path(fallback)


# --------------------------------------------------------------------------- #
# Submitter launch
# --------------------------------------------------------------------------- #
def _launch_submitter(output_dir: Path) -> tuple[Path, int]:
    """
    Show the real submitter dialog and let the xa11y sidecar click Export
    bundle. Returns (path-to-exported-bundle, sidecar-exit-code).
    """
    from qtpy import QtWidgets  # type: ignore[import-not-found]
    from qtpy.QtCore import QTimer  # type: ignore[import-not-found]
    from deadline.cinema4d_submitter.cinema4d_render_submitter import (  # type: ignore[import-not-found]
        _show_submitter,
    )
    from deadline.cinema4d_submitter.style import C4D_STYLE  # type: ignore[import-not-found]
    from deadline.client.config import set_setting  # type: ignore[import-not-found]

    # Steer the "Export bundle" output into our output dir so we can find
    # and upload the bundle deterministically.
    job_history_dir = output_dir / "job-history"
    job_history_dir.mkdir(parents=True, exist_ok=True)
    set_setting("settings.job_history_dir", str(job_history_dir))
    log(f"job_history_dir set to {job_history_dir}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(C4D_STYLE)

    # _show_submitter wants a temp dir to stage scene assets in (used for
    # job-attachment scene-with-assets export). Anywhere writable will do.
    temp_dir = output_dir / "scene-with-assets"
    temp_dir.mkdir(parents=True, exist_ok=True)

    dialog = _show_submitter(str(temp_dir), None)
    dialog.setStyleSheet(C4D_STYLE)

    # Spawn the xa11y sidecar.
    sidecar = Path(__file__).resolve().parent / "automate.py"
    if not sidecar.is_file():
        # When invoked from inside an OpenJD session, the file is just
        # next to driver.py (which run_test.py copied/staged).
        sidecar = Path(__file__).parent / "automate.py"
    py = _find_sidecar_python()
    log(f"sidecar python: {py}")
    log(f"sidecar script: {sidecar}")

    sidecar_log = output_dir / "automate.log"
    sidecar_proc = subprocess.Popen(
        [str(py), str(sidecar), str(os.getpid()), str(output_dir)],
        stdout=open(sidecar_log, "w", encoding="utf-8", buffering=1),
        stderr=subprocess.STDOUT,
    )
    log(f"sidecar pid: {sidecar_proc.pid} (log -> {sidecar_log})")

    sidecar_result: dict[str, int] = {}

    def _watch_sidecar():
        sidecar_result["exit"] = sidecar_proc.wait()
        log(f"sidecar exited with code {sidecar_result['exit']}")

    watcher = threading.Thread(target=_watch_sidecar, daemon=True)
    watcher.start()

    # Failsafe: if the sidecar never makes the dialog go away (e.g. xa11y
    # can't see the UI at all), close the dialog so exec_() returns and
    # the worker doesn't hang forever.
    HANG_TIMEOUT_S = 240

    def _force_close():
        log(f"force-close timer fired after {HANG_TIMEOUT_S}s — closing dialog")
        try:
            dialog.close()
        except Exception:
            traceback.print_exc()
        # Kill the sidecar too in case it's still polling.
        if sidecar_proc.poll() is None:
            try:
                sidecar_proc.kill()
            except Exception:
                pass

    timer = QTimer(dialog)
    timer.setSingleShot(True)
    timer.timeout.connect(_force_close)
    timer.start(HANG_TIMEOUT_S * 1000)

    log("entering dialog.exec_()")
    dialog.exec_()
    log("dialog.exec_() returned")
    timer.stop()

    # Give the sidecar a moment to finish writing artifacts.
    if sidecar_proc.poll() is None:
        try:
            sidecar_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log("sidecar still running after dialog closed; killing")
            sidecar_proc.kill()
            sidecar_proc.wait(timeout=5)
    watcher.join(timeout=5)
    exit_code = sidecar_result.get("exit", -1)

    # Tee the sidecar log to our own stdout so it lands in CloudWatch and
    # we don't have to wait for job-output upload to triage failures.
    if sidecar_log.is_file():
        log(f"--- begin {sidecar_log.name} ---")
        try:
            for line in sidecar_log.read_text(encoding="utf-8", errors="replace").splitlines():
                print(line, flush=True)
        except Exception:
            traceback.print_exc()
        log(f"--- end {sidecar_log.name} ---")
    tree = output_dir / "submitter-tree.txt"
    if tree.is_file():
        log(f"--- begin {tree.name} ({tree.stat().st_size} bytes) ---")
        try:
            text = tree.read_text(encoding="utf-8", errors="replace")
            # Cap at 30 KB so we don't blow out CloudWatch on huge dumps.
            if len(text) > 30_000:
                text = text[:30_000] + f"\n... [truncated, full file is {tree.stat().st_size} bytes]"
            for line in text.splitlines():
                print(line, flush=True)
        except Exception:
            traceback.print_exc()
        log(f"--- end {tree.name} ---")

    # Find the exported bundle dir under job_history_dir/<YYYY-mm>/.
    bundles = sorted(p for p in job_history_dir.glob("*/*") if p.is_dir())
    if not bundles:
        raise RuntimeError(
            f"No exported bundle found under {job_history_dir}. "
            f"Sidecar exit={exit_code}; check automate.log and submitter-tree.txt."
        )
    bundle_path = bundles[-1]
    log(f"exported bundle at: {bundle_path}")
    return bundle_path, exit_code


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    if len(sys.argv) < 2:
        print("usage: c4dpy driver.py <output-dir>", file=sys.stderr)
        return 2

    out_dir = Path(sys.argv[1]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"C4D version:     {c4d.GetC4DVersion()}")
    log(f"sys.executable:  {sys.executable}")
    log(f"output dir:      {out_dir}")
    log(f"sys.path[:6]:    {sys.path[:6]}")
    for mod in ("PySide6", "PySide6.QtWidgets", "shiboken6", "qtpy", "xa11y"):
        try:
            m = __import__(mod, fromlist=[""])
            log(f"import {mod}: OK ({getattr(m, '__file__', '?')})")
        except Exception as e:
            log(f"import {mod}: FAILED ({type(e).__name__}: {e})")

    try:
        import deadline.cinema4d_submitter as cs  # type: ignore[import-not-found]
    except Exception:
        traceback.print_exc()
        return 3
    log(f"submitter module: {cs.__file__}")

    scene_dir = out_dir / "scene"
    scene_path = _build_scene(scene_dir)
    log(f"saved scene: {scene_path}")

    bundle_path, sidecar_exit = _launch_submitter(out_dir)

    # Surface the bundle path for run_test.py via a sidecar file. Keeps
    # the contract trivial — no parsing of stdout.
    (out_dir / "bundle-path.txt").write_text(str(bundle_path), encoding="utf-8")
    (out_dir / "automate-exit.txt").write_text(str(sidecar_exit), encoding="utf-8")
    log(f"wrote bundle-path.txt -> {bundle_path}")
    log(f"automate sidecar exit code: {sidecar_exit}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("ERROR in driver:")
        traceback.print_exc()
        sys.exit(1)
