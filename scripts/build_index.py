#!/usr/bin/env python3
"""Build a static PEP 503 "simple" package index from a folder of wheels/sdists.

Used by the release workflow to publish an index on GitHub Pages, so that a
machine with this in uv.toml can run the bare `uv tool install cswapsync`:

    [[index]]
    name = "cswapsync"
    url = "https://hkoradiya123.github.io/CSwapSync/simple/"

Usage: build_index.py <dist-dir> <site-dir>
Writes <site-dir>/files/* (copies) and <site-dir>/simple/... with relative,
sha256-pinned links. Standard library only.
"""
from __future__ import annotations

import hashlib
import html
import re
import shutil
import sys
from pathlib import Path

PROJECT = "cswapsync"
EXTS = (".whl", ".tar.gz")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def build(dist: Path, site: Path) -> list[str]:
    files = sorted(p for p in dist.iterdir()
                   if p.name.endswith(EXTS) and normalize(p.name).startswith(PROJECT))
    if not files:
        raise SystemExit(f"no {PROJECT} distributions in {dist}")
    (site / "files").mkdir(parents=True, exist_ok=True)
    project_dir = site / "simple" / PROJECT
    project_dir.mkdir(parents=True, exist_ok=True)

    links = []
    for f in files:
        shutil.copyfile(f, site / "files" / f.name)
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        href = f"../../files/{f.name}#sha256={digest}"
        links.append(f'    <a href="{html.escape(href)}" data-requires-python="&gt;=3.10">'
                     f"{html.escape(f.name)}</a><br>")
    (project_dir / "index.html").write_text(
        "<!DOCTYPE html>\n<html><head><meta name=\"pypi:repository-version\" content=\"1.0\">"
        f"<title>Links for {PROJECT}</title></head><body>\n<h1>Links for {PROJECT}</h1>\n"
        + "\n".join(links) + "\n</body></html>\n", encoding="utf-8")
    (site / "simple" / "index.html").write_text(
        "<!DOCTYPE html>\n<html><head><meta name=\"pypi:repository-version\" content=\"1.0\">"
        f"<title>Simple index</title></head><body>\n<a href=\"{PROJECT}/\">{PROJECT}</a>\n"
        "</body></html>\n", encoding="utf-8")
    (site / ".nojekyll").write_text("")
    (site / "index.html").write_text(
        "<!DOCTYPE html>\n<html><body><h1>cswapsync package index</h1>"
        "<p>Add to uv.toml: <code>[[index]] name = \"cswapsync\" url = "
        "\"https://hkoradiya123.github.io/CSwapSync/simple/\"</code>, then "
        "<code>uv tool install cswapsync</code>.</p></body></html>\n", encoding="utf-8")
    return [f.name for f in files]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    for name in build(Path(sys.argv[1]), Path(sys.argv[2])):
        print("indexed", name)
