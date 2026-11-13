from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..models import Action, MemoryRecord
from ..policies import Capabilities


class Adapter(ABC):
    """Contract every memory backend must implement.

    ``list_memories`` must only return live records that the backend can mutate.
    Derived/index-only records (e.g. Hindsight observations) must be hidden by the
    adapter, because curating them is either unsupported or unsafe.
    """

    name = "base"
    capabilities = Capabilities()

    @abstractmethod
    def list_memories(self, limit: Optional[int] = None) -> List[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    def apply(self, action: Action) -> bool:
        """Apply one action. Return True on success; raise on failure."""
        raise NotImplementedError

    def restore(self, memory_id: str) -> bool:
        """Undo an invalidate/archive action. Return True on success."""
        raise NotImplementedError(f"{self.name} does not implement restore()")

    def recall(self, query: str, limit: int = 5) -> List[Dict]:
        """Return recall results for active-forgetting verification."""
        raise NotImplementedError(f"{self.name} does not implement recall()")

    def describe(self) -> Dict:
        return {
            "name": self.name,
            "capabilities": {
                "invalidate": self.capabilities.invalidate,
                "delete": self.capabilities.delete,
                "archive": self.capabilities.archive,
                "importance": self.capabilities.importance,
                "usage_signals": self.capabilities.usage_signals,
                "recall": self.capabilities.recall,
            },
        }
