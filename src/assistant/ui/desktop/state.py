from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WindowGeometry(BaseModel):
    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class DesktopWindowState(BaseModel):
    mode: Literal["compact", "full"] = "compact"
    primary_screen: Literal["dashboard", "assistant"] = "assistant"
    compact_pinned: bool = True
    compact_geometry: WindowGeometry | None = None
    full_geometry: WindowGeometry | None = None
