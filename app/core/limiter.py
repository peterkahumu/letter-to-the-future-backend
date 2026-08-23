"""
Per-IP rate limiting (E5).

Storage is in-process, so limits are per-replica. That is enough to stop
casual abuse; if you scale out, point slowapi at Redis so the budget is
shared across replicas.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, headers_enabled=True)
