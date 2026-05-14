# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Sidecar process that drives the Cinema 4D submitter Qt dialog via xa11y.

Runs *outside* c4dpy. xa11y under Windows UI Automation can't reliably
introspect the calling process from a worker thread (UIA marshals back to
the COM apartment of the host process, which is blocked inside the modal
dialog's exec_()), so we drive the UI from a separate Python process.

Usage:
    python3 automate.py <c4dpy-pid> <output-dir>

Side effects:
    * Saves <output-dir>/submitter-window.png on success (and a fallback
      full-screen capture if element capture fails).
    * Saves <output-dir>/submitter-tree.txt with diagnostic output if
      anything goes wrong.
    * Exit code 0 on success, non-zero on failure.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

# Match prefix instead of full title so version drift doesn't break the test.
DIALOG_TITLE_PREFIX = "Deadline Cloud "
SUCCESS_TITLE = "Cinema4D job submission"


def log(msg: str) -> None:
    print(f"[automate] {msg}", flush=True)


def _dump_diagnostics(out_path: Path, target_pid: int) -> None:
    chunks: list[str] = []
    try:
        import xa11y  # type: ignore[import-not-found]
    except Exception as e:
        out_path.write_text(f"xa11y import failed: {e}\n", encoding="utf-8")
        return

    chunks.append(f"=== target pid: {target_pid} ===")
    try:
        a = xa11y.App.by_pid(target_pid, timeout=2.0)
        chunks.append(a.dump())
    except Exception as e:
        chunks.append(f"App.by_pid({target_pid}) failed: {type(e).__name__}: {e}")

    chunks.append("")
    chunks.append("=== xa11y.App.list() (depth 3) ===")
    try:
        for app in xa11y.App.list():
            chunks.append(f"- name={app.name!r} pid={app.pid}")
            try:
                chunks.append(app.dump(max_depth=3))
            except Exception as e:
                chunks.append(f"  dump failed: {type(e).__name__}: {e}")
    except Exception as e:
        chunks.append(f"App.list() failed: {type(e).__name__}: {e}")

    out_path.write_text("\n".join(chunks), encoding="utf-8")
    log(f"wrote diagnostics to {out_path}")


def _attach(target_pid: int, deadline: float):
    """
    Find an xa11y App handle for the c4dpy process. On Windows SMF workers
    UI Automation may take a while to see the new top-level window, so
    poll until deadline.
    """
    import xa11y  # type: ignore[import-not-found]
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return xa11y.App.by_pid(target_pid, timeout=1.0)
        except Exception as e:
            last_err = e
        try:
            for app in xa11y.App.list():
                try:
                    if app.pid == target_pid:
                        return app
                    if app.locator(f"window[name^='{DIALOG_TITLE_PREFIX}']").exists():
                        log(f"attached via App.list() match: name={app.name!r} pid={app.pid}")
                        return app
                except Exception:
                    continue
        except Exception as e:
            last_err = e
        time.sleep(1.0)
    raise RuntimeError(
        f"xa11y could not attach to pid={target_pid} or find the submitter window. "
        f"last_err={last_err!r}"
    )


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: automate.py <c4dpy-pid> <output-dir>", file=sys.stderr)
        return 2
    target_pid = int(sys.argv[1])
    output_dir = Path(sys.argv[2]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"target pid: {target_pid}")
    log(f"output dir: {output_dir}")
    log(f"sys.executable: {sys.executable}")

    try:
        import xa11y  # type: ignore[import-not-found]
        log(f"xa11y from: {xa11y.__file__}")
    except Exception:
        traceback.print_exc()
        return 3

    try:
        deadline_ts = time.monotonic() + 90.0
        app = _attach(target_pid, deadline_ts)
        log(f"attached: name={app.name!r} pid={app.pid}")

        dlg = app.locator(f"window[name^='{DIALOG_TITLE_PREFIX}']")
        dlg.wait_visible(timeout=60.0)
        log(f"dialog visible (name starts with {DIALOG_TITLE_PREFIX!r})")

        # Let the layout settle a moment before snapping.
        time.sleep(1.5)

        screenshot_path = output_dir / "submitter-window.png"
        try:
            xa11y.screenshot(element=dlg.element()).save_png(str(screenshot_path))
            log(f"saved element screenshot: {screenshot_path}")
        except Exception:
            log("element screenshot failed, falling back to full-screen capture")
            traceback.print_exc()
            try:
                xa11y.screenshot().save_png(str(screenshot_path))
                log(f"saved full-screen screenshot: {screenshot_path}")
            except Exception:
                log("fallback screenshot also failed")
                traceback.print_exc()

        export_btn = dlg.descendant("button[name='Export bundle']")
        export_btn.wait_visible(timeout=15.0)
        log("clicking Export bundle")
        export_btn.press()

        success = app.locator(f"window[name='{SUCCESS_TITLE}']")
        success.wait_visible(timeout=60.0)
        log("success dialog visible")
        ok_btn = success.descendant("button[name='OK']")
        ok_btn.wait_visible(timeout=15.0)
        log("clicking OK on success dialog")
        ok_btn.press()

        log("automation done")
        return 0
    except Exception:
        log("automation failed:")
        traceback.print_exc()
        try:
            _dump_diagnostics(output_dir / "submitter-tree.txt", target_pid)
        except Exception:
            log("diagnostics dump failed")
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
