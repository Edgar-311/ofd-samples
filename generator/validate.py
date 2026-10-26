#!/usr/bin/env python3
"""Sanity checks on samples/ and expected/ that do not depend on the generator's own code.

- Every sample has an expected output, and the other way round.
- Every XML part of a conforming sample parses with a strict XML parser, and OFD.xml
  has an OFD root element.
- Expected outputs have the fields the README documents.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE_KEYS = {"id", "width", "height", "readable", "text", "text_objects"}
OBJECT_KEYS = {"id", "text", "size", "from_template", "runs"}


def main() -> int:
    problems: list[str] = []
    samples = {path.stem for path in (ROOT / "samples").glob("*.ofd")}
    expected = {path.stem for path in (ROOT / "expected").glob("*.json")}
    problems += [f"{name}: no expected output" for name in sorted(samples - expected)]
    problems += [f"{name}: no sample file" for name in sorted(expected - samples)]

    for name in sorted(samples & expected):
        data = json.loads((ROOT / "expected" / f"{name}.json").read_text(encoding="utf-8"))
        for key in ("name", "description", "conforming", "features", "doc_info", "pages"):
            if key not in data:
                problems.append(f"{name}: expected output has no '{key}'")
        for number, page in enumerate(data.get("pages", []), start=1):
            if set(page) != PAGE_KEYS:
                problems.append(f"{name}: page {number} has keys {sorted(page)}")
            for obj in page.get("text_objects", []):
                if set(obj) != OBJECT_KEYS:
                    problems.append(f"{name}: page {number} object has keys {sorted(obj)}")

        with zipfile.ZipFile(ROOT / "samples" / f"{name}.ofd") as archive:
            if "OFD.xml" not in archive.namelist():
                problems.append(f"{name}: OFD.xml is not at the package root")
            if not data.get("conforming"):
                continue
            for entry in archive.namelist():
                if not entry.endswith(".xml"):
                    continue
                try:
                    root = ET.fromstring(archive.read(entry))
                except ET.ParseError as exc:
                    problems.append(f"{name}: {entry} is not well-formed: {exc}")
                    continue
                if entry == "OFD.xml" and root.tag.rsplit("}", 1)[-1] != "OFD":
                    problems.append(f"{name}: OFD.xml root is {root.tag}")

    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{len(samples)} samples validated, {len(problems)} problems.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
