# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
c4dpy-side driver for the E2E test.

Runs inside Cinema 4D's bundled Python (c4dpy). Builds a tiny scene, launches
the real Qt submitter dialog (the same one users see when they click
"Submit to AWS Deadline Cloud"), then drives it with xa11y to:

  * screenshot the submitter window
  * click the "Export bundle" button
  * dismiss the success message dialog

Artifacts (screenshot, exported bundle, a11y tree on failure) are written
under <output-dir> so they can be uploaded as job outputs.

Usage (called by run_test.py):
    c4dpy.exe driver.py <output-dir>
"""
from __future__ import annotations

import os
import sys
import threading
import time
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
# xa11y automation thread
# --------------------------------------------------------------------------- #
# When SubmitterInfo is supplied, the deadline-cloud client formats the title
# as ``Deadline Cloud Cinema4D Submitter <version>``. Match by prefix so
# version drift doesn't break the test.
DIALOG_TITLE_PREFIX = "Deadline Cloud "


def _dump_tree_safely(out_path: Path) -> None:
    """Best-effort accessibility tree dump for failure triage."""
    try:
        import xa11y  # type: ignore[import-not-found]
        a = xa11y.App.by_pid(os.getpid(), timeout=2.0)
        out_path.write_text(a.dump(), encoding="utf-8")
        log(f"dumped a11y tree to {out_path}")
    except Exception:
        log("failed to dump a11y tree")
        traceback.print_exc()


def _automate_submitter(output_dir: Path, errors: list[str]) -> None:
    """
    Drive the Qt submitter dialog from a worker thread:
    screenshot -> click "Export bundle" -> dismiss success message.

    On any failure, dumps the a11y tree to <output-dir>/submitter-tree.txt.
    """
    try:
        import xa11y  # type: ignore[import-not-found]

        # The dialog is hosted inside the c4dpy process.
        app = xa11y.App.by_pid(os.getpid(), timeout=30.0)
        log(f"xa11y attached to pid={os.getpid()} app={app.name!r}")

        dlg = app.locator(f"window[name^='{DIALOG_TITLE_PREFIX}']")
        dlg.wait_visible(timeout=30.0)
        log(f"submitter window visible (name starts with {DIALOG_TITLE_PREFIX!r})")

        # Give the layout a moment to settle before snapping.
        time.sleep(1.0)

        win_elem = dlg.element()
        screenshot_path = output_dir / "submitter-window.png"
        xa11y.screenshot(element=win_elem).save_png(str(screenshot_path))
        log(f"saved screenshot: {screenshot_path}")

        export_btn = dlg.locator("button[name='Export bundle']")
        export_btn.wait_visible(timeout=10.0)
        log("clicking Export bundle")
        export_btn.press()

        # on_export_bundle pops a QMessageBox.information whose title is
        # "Cinema4D job submission". Scope the OK click to that window so
        # we don't accidentally hit an OK button elsewhere in the tree.
        success = app.locator("window[name='Cinema4D job submission']")
        success.wait_visible(timeout=30.0)
        log("success dialog visible")
        ok_btn = success.descendant("button[name='OK']")
        ok_btn.wait_visible(timeout=10.0)
        log("clicking OK on success dialog")
        ok_btn.press()

        log("automation thread done")
    except Exception:
        traceback.print_exc()
        errors.append(traceback.format_exc())
        _dump_tree_safely(output_dir / "submitter-tree.txt")


# --------------------------------------------------------------------------- #
# Submitter launch
# --------------------------------------------------------------------------- #
def _launch_submitter(output_dir: Path, scene_dir: Path) -> Path:
    """
    Show the real submitter dialog and let the xa11y thread click Export
    bundle. Returns the path to the exported bundle on disk.
    """
    from qtpy import QtWidgets  # type: ignore[import-not-found]
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

    errors: list[str] = []
    automation = threading.Thread(
        target=_automate_submitter,
        args=(output_dir, errors),
        daemon=True,
    )
    automation.start()

    log("entering dialog.exec_()")
    dialog.exec_()
    log("dialog.exec_() returned")
    automation.join(timeout=15)

    if errors:
        raise RuntimeError(
            "UI automation thread reported errors:\n" + "\n---\n".join(errors)
        )

    # Find the exported bundle dir under job_history_dir/<YYYY-mm>/.
    bundles = sorted(p for p in job_history_dir.glob("*/*") if p.is_dir())
    if not bundles:
        _dump_tree_safely(output_dir / "submitter-tree.txt")
        raise RuntimeError(
            f"No exported bundle found under {job_history_dir}. Did Export bundle run?"
        )
    bundle_path = bundles[-1]
    log(f"exported bundle at: {bundle_path}")
    return bundle_path


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

    bundle_path = _launch_submitter(out_dir, scene_dir)

    # Surface the bundle path for run_test.py via a sidecar file. Keeps
    # the contract trivial — no parsing of stdout.
    (out_dir / "bundle-path.txt").write_text(str(bundle_path), encoding="utf-8")
    log(f"wrote bundle-path.txt -> {bundle_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("ERROR in driver:")
        traceback.print_exc()
        sys.exit(1)
