#!/usr/bin/env python3
"""Extract exact runtime versions from a generated hashed requirements lock."""

from pathlib import Path
import re
import sys

for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.match(r"^([A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;\\]+)", line)
    if match:
        print(match.group(1))
