"""
Shared helper for describing pyepics return values in JSON-friendly terms.

Kept in its own tiny module so both the HTTP response builder and the
monitoring layer can use it without the HTTP side pulling in
pyepics/ophyd at import time.
"""

from typing import Any, List, Optional, Tuple

import numpy as np


def describe_array(raw: Any) -> Tuple[List[int], Optional[str], int, int]:
    """Return ``(shape, dtype_str, ndim, nbytes)`` for a pyepics value.

    Scalars and non-array types collapse to ``([], None, 0, 0)``.
    numpy scalars expose ``.dtype`` / ``.nbytes`` directly — no zero-d
    array allocation.
    """
    if isinstance(raw, np.ndarray):
        return list(raw.shape), raw.dtype.str, int(raw.ndim), int(raw.nbytes)
    if isinstance(raw, (np.number, np.bool_)):
        return [], raw.dtype.str, 0, int(raw.nbytes)
    return [], None, 0, 0
