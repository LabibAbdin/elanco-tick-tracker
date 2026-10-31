"""Response models (used for validation and for the generated OpenAPI docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SightingOut(BaseModel):
    id: str = Field(examples=["02WNholuSg6ndCk4c1dA"])
    observed_at: datetime
    city: str = Field(examples=["Manchester"])
    region: str = Field(examples=["North West"])
    nation: str = Field(examples=["England"])
    latitude: float | None = None
    longitude: float | None = None
    species: str = Field(examples=["Marsh tick"])
    latin_name: str = Field(examples=["Ixodes apronophorus"])
    quality_flags: list[str] = Field(default_factory=list, description="Repairs applied during cleaning")
    duplicate_of: str | None = Field(
        None, description="Set when this looks like a re-report of another sighting"
    )


class SightingDetail(SightingOut):
    source: str
    is_active: bool
    raw_location: str | None
    raw_species: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    updated_at: datetime
    probable_duplicates: list[str] = Field(default_factory=list)


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    sort: str
    order: str
    filters: dict[str, Any]


class PageLinks(BaseModel):
    self: str
    next: str | None = None
    prev: str | None = None


class SightingPage(BaseModel):
    data: list[SightingOut]
    meta: PageMeta
    links: PageLinks


class ErrorBody(BaseModel):
    code: str = Field(examples=["unknown_location"])
    message: str = Field(examples=["Unknown location 'Manchster'."])
    details: dict[str, Any] = Field(default_factory=dict, examples=[{"suggestions": ["Manchester"]}])
    request_id: str | None = Field(None, examples=["9f1c2d0e6b7a4c1d"])


class ErrorResponse(BaseModel):
    error: ErrorBody


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    422: {"model": ErrorResponse, "description": "Invalid parameters (with suggestions where possible)"},
    500: {"model": ErrorResponse, "description": "Unexpected server error"},
}
