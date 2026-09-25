"""Typst rendering: JSON data file + template -> PDF, and a page count.

Templates live in templates/ and read their data with
`json(sys.inputs.at("data"))`, so user text is inserted as plain strings and
can never be evaluated as Typst markup.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import config


class RenderError(RuntimeError):
    pass


def _typst() -> str:
    exe = shutil.which("typst")
    if not exe:
        raise RenderError("typst is not installed (brew install typst)")
    return exe


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RenderError(proc.stderr.strip() or f"typst exited {proc.returncode}")
    return proc.stdout


def render(template: str, data: dict[str, Any], out_pdf: Path) -> int:
    """Render `templates/<template>` with `data` to `out_pdf`; return the page count."""
    root = config.project_root()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    data_file = out_pdf.with_suffix(".json")
    data_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    common = [
        "--root", str(root),
        # Fonts you drop in templates/fonts/ (e.g. Carlito or Calibri) win over system fallbacks.
        "--font-path", str(root / "templates" / "fonts"),
        "--input", f"data=/{data_file.resolve().relative_to(root.resolve()).as_posix()}",
    ]
    template_path = str(root / "templates" / template)
    exe = _typst()
    _run([exe, "compile", *common, template_path, str(out_pdf)])
    pages = _run([exe, "eval", *common, "--in", template_path, "query(<page-count>).first().value"])
    try:
        return int(pages.strip())
    except ValueError as exc:
        raise RenderError(f"could not read page count: {pages!r}") from exc


def upload_name(full_name: str, kind: str = "Resume") -> str:
    """`Firstname_Lastname_Resume.pdf` -- the file name recruiters see."""
    parts = [re.sub(r"[^A-Za-z0-9\-]", "", p) for p in full_name.split()]
    parts = [p for p in parts if p]
    stem = f"{parts[0]}_{parts[-1]}" if len(parts) >= 2 else (parts[0] if parts else "Candidate")
    return f"{stem}_{kind}.pdf"


def copy_for_upload(pdf: Path, full_name: str, kind: str = "Resume") -> Path:
    target = pdf.parent / upload_name(full_name, kind)
    shutil.copyfile(pdf, target)
    return target
