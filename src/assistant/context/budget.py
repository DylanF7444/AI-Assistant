from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_items: int = Field(default=20, ge=1)
    max_item_value_chars: int = Field(default=1000, ge=1)
    max_total_value_chars: int = Field(default=4000, ge=1)
    include_sensitive: bool = False
    include_provider_errors: bool = False

    def value_size(self, value: Any) -> int:
        try:
            return len(json.dumps(value, default=str, sort_keys=True))
        except TypeError:
            return len(str(value))
