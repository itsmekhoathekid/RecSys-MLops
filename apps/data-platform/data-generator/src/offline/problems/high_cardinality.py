from uuid import UUID

from randomness import DeterministicIds


class HighCardinalityProblem:
    """Allocate a distinct deterministic identifier for every entity occurrence."""

    def __init__(self, seed: int):
        self.ids = DeterministicIds(seed)

    def next_id(self, entity_type: str) -> UUID:
        return self.ids.next(entity_type)
