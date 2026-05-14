# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
c4dpy-side driver for the E2E test.

Runs inside Cinema 4D's bundled Python (c4dpy). Builds a tiny scene and
calls the same code path the GUI's "Export bundle" button triggers, writing
the bundle to the output dir passed as argv[1].

Usage (called by run_test.py):
    c4dpy.exe driver.py <output-bundle-dir>
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import c4d  # type: ignore[import-not-found]


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


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: c4dpy driver.py <output-bundle-dir>", file=sys.stderr)
        return 2

    out_dir = Path(sys.argv[1]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[driver] C4D version:     {c4d.GetC4DVersion()}")
    print(f"[driver] sys.executable:  {sys.executable}")
    print(f"[driver] output bundle:   {out_dir}")

    try:
        import deadline.cinema4d_submitter as cs  # type: ignore[import-not-found]
    except Exception:
        traceback.print_exc()
        return 3
    print(f"[driver] submitter module: {cs.__file__}")

    scene_dir = out_dir.parent / "scene"
    scene_path = _build_scene(scene_dir)
    print(f"[driver] saved scene: {scene_path}")

    try:
        from deadline.cinema4d_submitter.integ_test_helpers import (  # type: ignore[import-not-found]
            internal_create_job_bundle,
        )
        internal_create_job_bundle(str(out_dir))
    except Exception:
        traceback.print_exc()
        return 4

    print(f"[driver] bundle files: {sorted(p.name for p in out_dir.iterdir())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
