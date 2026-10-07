"""Tiny CHANGELOG.md reader for the release job: versions | notes <v> | ref <v>."""

import re
import sys
from pathlib import Path

text = Path("CHANGELOG.md").read_text(encoding="utf-8")
sections = re.split(r"^## \[", text, flags=re.M)[1:]
parsed = {}
for sec in sections:
    version, _, body = sec.partition("]")
    body = body.split("\n", 1)[1] if "\n" in body else ""
    m = re.search(r"<!--\s*ref:\s*([0-9a-f]{7,40})\s*-->", body)
    parsed[version.strip()] = (re.sub(r"<!--.*?-->\n?", "", body).strip(), m.group(1) if m else "")

cmd = sys.argv[1]
if cmd == "versions":
    print("\n".join(reversed(list(parsed))))  # oldest first
elif cmd == "notes":
    print(parsed[sys.argv[2]][0])
elif cmd == "ref":
    print(parsed[sys.argv[2]][1])
