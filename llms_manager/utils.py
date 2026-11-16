import warnings
from uuid import uuid4

from django.conf import settings


def revision_upload_to(instance, filename) -> str:
    return f"llms_manager/{uuid4().hex}.txt"


def get_cache_timeout() -> int | None:
    """Return the cache timeout, disabling caching for invalid settings."""
    timeout = getattr(settings, "LLMS_MANAGER_CACHE_TIMEOUT", 60)
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0
    ):
        warnings.warn(
            "LLMS_MANAGER_CACHE_TIMEOUT must be a non-negative integer or None; "
            "caching is disabled.",
            RuntimeWarning,
            stacklevel=1,
        )
        return 0
    return timeout
