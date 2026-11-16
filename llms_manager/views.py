from hashlib import sha256

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_safe

from llms_manager.models import Publication
from llms_manager.utils import get_cache_timeout


@require_safe
def published_file(request, filename: str, directory: str = "") -> HttpResponse:
    """Serve llms.txt or llms-full.txt for the matching publication.

    Match the filename and directory, using "/" when the directory is empty.
    Cache revision text while resolving the publication on every request.
    """
    publication = get_object_or_404(
        Publication.objects.select_related("revision"),
        directory=f"/{directory}/" if directory else "/",
        filename=filename,
    )
    revision = publication.revision
    content = ""
    if revision is not None:
        timeout = get_cache_timeout()
        if not timeout:
            content = revision.read_content()
        else:
            file_key = sha256(revision.file.name.encode("utf-8")).hexdigest()
            content = cache.get_or_set(
                f"llms_manager:revision:{revision.pk}:{file_key}",
                revision.read_content,
                timeout=timeout,
            )
    return HttpResponse(
        content,
        content_type="text/plain; charset=utf-8",
    )


published_file.login_required = False
