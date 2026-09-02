from __future__ import annotations

from typing import Any, Callable

from .state import RelayState


class RelayRouter:
    def __init__(self, state: RelayState, assets: Callable[[], list[dict[str, Any]]]): self.state = state; self.assets = assets

    def choose(self, model: str, strategy: str, session_key: str = "", affinity_ttl: float = 3600) -> list[dict[str, Any]]:
        rows = self.state.candidates(model, strategy if strategy in {"fill_first", "round_robin"} else "fill_first", self.assets())
        bound = self.state.session_account(session_key, affinity_ttl)
        if bound:
            rows.sort(key=lambda row: 0 if int(row["account_id"]) == bound else 1)
        return rows
