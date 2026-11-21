from hashlib import sha256

from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_safe

from llms_manager.exceptions import RevisionFileError
from llms_manager.models import Publication
from llms_manager.utils import get_cache_timeout


@require_safe
def published_file(request, filename: str, directory: str = "") -> HttpResponse:
    """Serve llms.txt or llms-full.txt for the matching publication.

    Match the filename and directory, using "/" when the directory is empty.
    Cache successful publication text by its public address until expiry.
    """
    directory = f"/{directory}/" if directory else "/"
    timeout = get_cache_timeout()
    address_key = sha256(f"{directory}{filename}".encode()).hexdigest()
    cache_key = f"llms_manager:publication:{address_key}"
    content = cache.get(cache_key) if timeout else None
    if content is None:
        publication = get_object_or_404(
            Publication.objects.select_related("revision"),
            directory=directory,
            filename=filename,
        )
        try:
            content = (
                publication.revision.read_content()
                if publication.revision is not None
                else ""
            )
        except RevisionFileError:
            return HttpResponseBadRequest(
                "The revision file is corrupted or missing.",
                content_type="text/plain; charset=utf-8",
            )
        if timeout:
            cache.set(cache_key, content, timeout=timeout)
    return HttpResponse(
        content,
        content_type="text/plain; charset=utf-8",
    )


published_file.login_required = False
