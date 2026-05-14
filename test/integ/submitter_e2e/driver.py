# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
c4dpy-side driver for the E2E test.

Runs inside Cinema 4D's bundled Python (c4dpy). Builds a tiny scene, launches
the real Qt submitter dialog (the same one users see when they click
"Submit to AWS Deadline Cloud"), then drives it with Qt directly to:

  * screenshot the submitter window via QWidget.grab()
  * click the "Export bundle" button
  * dismiss the success message dialog

We drive the UI from inside the same Qt process (rather than via an
accessibility API like xa11y/UIA) because Deadline Cloud Service Managed
Fleet Windows workers run as a service in Session 0 with no interactive
desktop — Windows UI Automation returns an empty tree there. Qt itself
doesn't care about the desktop session, so widget access works fine.

Usage (called by run_test.py):
    c4dpy.exe driver.py <output-dir>
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Optional

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
# UI automation via Qt
# --------------------------------------------------------------------------- #
def _save_widget_screenshot(widget, path: Path) -> bool:
    """
    Snapshot a widget via Qt's QWidget.grab() — works without a desktop
    session because Qt renders the widget itself rather than reading from
    the Windows compositor. Returns True on success.
    """
    try:
        pixmap = widget.grab()
        ok = bool(pixmap.save(str(path), "PNG"))
        log(f"saved screenshot ({path.stat().st_size} bytes): {path} ok={ok}")
        return ok
    except Exception:
        log("widget screenshot failed")
        traceback.print_exc()
        return False


def _drive_dialog(dialog, output_dir: Path, errors: list[str]) -> None:
    """
    Drive the dialog to the "Export bundle" path. Runs as a QTimer
    callback inside the dialog's own event loop.
    """
    try:
        log("driving submitter: snapping screenshot before clicking Export bundle")
        # Make sure the dialog has had a paint event before grabbing.
        from qtpy.QtWidgets import QApplication  # type: ignore[import-not-found]
        QApplication.processEvents()
        _save_widget_screenshot(dialog, output_dir / "submitter-window.png")

        # The export button is wired up at construction time:
        #   self.export_bundle_button = QPushButton(tr("Export bundle"))
        #   self.export_bundle_button.clicked.connect(self.on_export_bundle)
        button = getattr(dialog, "export_bundle_button", None)
        if button is None:
            raise RuntimeError(
                "dialog has no export_bundle_button attribute; submitter API changed?"
            )
        log(f"clicking export_bundle_button (text={button.text()!r})")
        button.click()
        log("on_export_bundle returned (will close success dialog from event loop)")
    except Exception:
        traceback.print_exc()
        errors.append(traceback.format_exc())
        try:
            dialog.close()
        except Exception:
            pass


def _install_messagebox_dismisser(errors: list[str]) -> None:
    """
    on_export_bundle pops a QMessageBox.information(...) on success which
    is modal — it would block our QTimer callback chain. Replace it with
    a no-op for the duration of the test so the dialog closes cleanly.

    QMessageBox.critical is left intact so failure paths still surface.
    """
    try:
        from qtpy.QtWidgets import QMessageBox  # type: ignore[import-not-found]
        original = QMessageBox.information

        def _silent_information(*args, **kwargs):  # type: ignore[no-untyped-def]
            try:
                # args may be (parent, title, text, ...).
                title = args[1] if len(args) > 1 else "?"
                text = args[2] if len(args) > 2 else "?"
                log(f"QMessageBox.information suppressed: {title!r} / {text!r}")
            except Exception:
                pass
            return QMessageBox.Ok

        QMessageBox.information = staticmethod(_silent_information)  # type: ignore[assignment]
        log("QMessageBox.information patched to no-op")
    except Exception:
        log("failed to patch QMessageBox.information; success popup may block")
        traceback.print_exc()
        errors.append(traceback.format_exc())


# --------------------------------------------------------------------------- #
# Submitter launch
# --------------------------------------------------------------------------- #
def _launch_submitter(output_dir: Path) -> Path:
    """
    Show the real submitter dialog and drive Export bundle via Qt.
    Returns the path to the exported bundle on disk.
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
    log(f"dialog window title: {dialog.windowTitle()!r}")

    errors: list[str] = []
    _install_messagebox_dismisser(errors)

    # Schedule the click+screenshot from within the dialog's own event
    # loop so widgets are constructed and painted before we touch them.
    drive_timer = QTimer(dialog)
    drive_timer.setSingleShot(True)
    drive_timer.timeout.connect(lambda: _drive_dialog(dialog, output_dir, errors))
    drive_timer.start(2000)  # 2s lets the deadline-cloud auth probe settle

    # Failsafe: if anything goes wrong and the dialog never closes, kill
    # the modal so the worker doesn't hang.
    HANG_TIMEOUT_S = 90

    def _force_close():
        log(f"force-close timer fired after {HANG_TIMEOUT_S}s — closing dialog")
        try:
            dialog.close()
        except Exception:
            traceback.print_exc()

    hang_timer = QTimer(dialog)
    hang_timer.setSingleShot(True)
    hang_timer.timeout.connect(_force_close)
    hang_timer.start(HANG_TIMEOUT_S * 1000)

    log("entering dialog.exec_()")
    dialog.exec_()
    log("dialog.exec_() returned")
    drive_timer.stop()
    hang_timer.stop()

    if errors:
        raise RuntimeError(
            "UI driver reported errors:\n" + "\n---\n".join(errors)
        )

    # Find the exported bundle dir under job_history_dir/<YYYY-mm>/.
    bundles = sorted(p for p in job_history_dir.glob("*/*") if p.is_dir())
    if not bundles:
        raise RuntimeError(
            f"No exported bundle found under {job_history_dir}. "
            "Did Export bundle actually run?"
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
    for mod in ("PySide6", "PySide6.QtWidgets", "shiboken6", "qtpy"):
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

    bundle_path = _launch_submitter(out_dir)
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
