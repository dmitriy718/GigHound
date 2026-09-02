"""Docs drift check (P4-7): every APIRouter prefix in the app must appear in
docs/api-contract.md. Coarse by design — prefix-level string presence only,
so wording/layout changes in the doc don't break the build.
"""
import importlib
import pkgutil
from pathlib import Path

from fastapi import APIRouter

import app.routers as routers_pkg

DOC = Path(__file__).resolve().parents[2] / "docs" / "api-contract.md"

# Prefixes intentionally left out of the public contract doc (internal-only).
ALLOWLIST: set[str] = set()


def _router_prefixes() -> set[str]:
    prefixes: set[str] = set()
    for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"app.routers.{mod_info.name}")
        for obj in vars(module).values():
            if isinstance(obj, APIRouter) and obj.prefix:
                prefixes.add(obj.prefix)
    return prefixes


def test_router_prefixes_documented():
    doc_text = DOC.read_text(encoding="utf-8")
    undocumented = {
        prefix
        for prefix in _router_prefixes()
        if prefix not in ALLOWLIST and prefix not in doc_text
    }
    assert not undocumented, (
        f"router prefixes missing from {DOC}: {sorted(undocumented)}"
    )
