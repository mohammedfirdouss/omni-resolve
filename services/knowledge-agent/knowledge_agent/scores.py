"""Pure similarity-score normalization (Property 5 target).

Raw cosine similarity from the vector store may legitimately fall outside
[0.0, 1.0] (cosine similarity spans [-1, 1]) and mocked/degenerate responses
may carry NaN or infinities. Every score persisted to the State_Store must be
clamped into the closed interval [0.0, 1.0] (Requirement 3.7).
"""

from __future__ import annotations

import math


def normalize_similarity_score(raw: float) -> float:
    """Clamp a raw similarity score into the closed interval [0.0, 1.0].

    - NaN maps to 0.0 (no meaningful similarity signal).
    - Values below 0.0 (including -inf) map to 0.0.
    - Values above 1.0 (including +inf) map to 1.0.
    - In-range values pass through unchanged.
    """
    value = float(raw)
    if math.isnan(value):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
