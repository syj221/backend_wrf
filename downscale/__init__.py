"""High-resolution downscaling task domain.

This package deliberately does not share the normal WPS/WRF task state machine.
It owns the scientific contract and the expensive-task admission gate; a deployed
remote runner may consume only the immutable run plan it writes.
"""

from .router import build_router
from .service import DownscaleService

__all__ = ["DownscaleService", "build_router"]
