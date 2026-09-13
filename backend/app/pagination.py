"""Shared bounded list query parameters; defaults remain usable by direct callers."""

from typing import Annotated
from fastapi import Query

PageLimit = Annotated[int, Query(ge=1, le=500)]
PageOffset = Annotated[int, Query(ge=0, le=100000)]
