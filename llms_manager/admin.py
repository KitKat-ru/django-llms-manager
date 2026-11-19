from difflib import HtmlDiff
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import admin, messages
from django.contrib.admin.templatetags.admin_urls import add_preserved_filters
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import escape, format_html
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_safe

from llms_manager.exceptions import PublicationConflict, RevisionFileError
from llms_manager.forms import (
    DocumentSelectionForm,
    EditorForm,
    PublicationForm,
    PublishForm,
    RevisionSelectionForm,
    UploadForm,
)
from llms_manager.models import Document, Publication, Revision


def _read_revision_content(revision: Revision | None) -> tuple[str, str | None]:
    """Read revision text or return an error for a missing or corrupted file."""
    if revision is None:
        return "", None
    try:
        return revision.read_content(), None
    except RevisionFileError as error:
        return "", str(error)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    """Manage named documents, content editing, and immutable revision history."""

    list_display = ("name", "editor_link")
    change_form_template = "admin/llms_manager/document_change_form.html"

    @admin.display(description=gettext_lazy("Editor"))
    def editor_link(self, obj: Document) -> SafeString:
        return format_html(
            '<a href="{url}">{label}</a>',
            url=self._url(action="editor", document_id=obj.pk),
            label=_("Editor"),
        )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_urls(self) -> list[URLPattern]:
        wrap = self.admin_site.admin_view
        return [
            path(
                "<int:document_id>/editor/",
                wrap(self.editor_view),
                name="llms_manager_document_editor",
            ),
            path(
                "<int:document_id>/revisions/",
                wrap(self.revision_list_view),
                name="llms_manager_document_revisions",
            ),
            path(
                "<int:document_id>/revisions/<int:revision_id>/",
                wrap(self.revision_view),
                name="llms_manager_document_revision",
            ),
            path(
                "<int:document_id>/revisions/<int:revision_id>/edit/",
                wrap(self.editor_view),
                name="llms_manager_document_edit_revision",
            ),
            path(
                "<int:document_id>/revisions/<int:revision_id>/preview/",
                wrap(self.preview_view),
                name="llms_manager_document_preview",
            ),
        ] + super().get_urls()

    def _url(self, action: str, **kwargs: int) -> str:
        return reverse(
            f"{self.admin_site.name}:llms_manager_document_{action}",
            kwargs=kwargs,
        )

    def _require_permission(self, request: HttpRequest, change: bool = False) -> None:
        allowed = (
            self.has_change_permission(request)
            if change
            else self.has_view_or_change_permission(request)
        )
        if not allowed:
            raise PermissionDenied

    def _revision(self, document_id: int, revision_id: int) -> Revision:
        return get_object_or_404(
            Revision.objects.select_related("created_by", "document"),
            pk=revision_id,
            document_id=document_id,
        )

    def _render(
        self, request: HttpRequest, template: str, context: dict[str, object]
    ) -> TemplateResponse:
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            f"admin/llms_manager/{template}.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.opts,
                "has_change_permission": self.has_change_permission(request),
                **context,
            },
        )

    @method_decorator(require_http_methods(["GET", "POST"]))
    def editor_view(
        self,
        request: HttpRequest,
        document_id: int,
        revision_id: int | None = None,
    ) -> HttpResponse:
        self._require_permission(request, change=request.method == "POST")
        document = get_object_or_404(Document, pk=document_id)
        source = (
            self._revision(document.pk, revision_id)
            if revision_id is not None
            else None
        )
        upload_form = UploadForm()
        file_error = None

        if request.method == "POST":
            editor_form = EditorForm(request.POST)
            if "import_file" in request.POST:
                upload_form = UploadForm(request.POST, request.FILES)
                if upload_form.is_valid():
                    editor_form = EditorForm(
                        initial={"content": upload_form.cleaned_data["content"]}
                    )
                    upload_form = UploadForm()
                    self.message_user(
                        request,
                        _("File loaded into the editor. Save it to create a revision."),
                    )
            elif editor_form.is_valid():
                revision = Revision.create_from_content(
                    document, editor_form.cleaned_data["content"], request.user
                )
                self.message_user(
                    request, _("Revision saved. Review it before publishing.")
                )
                return HttpResponseRedirect(
                    self._url(
                        action="revision",
                        document_id=document.pk,
                        revision_id=revision.pk,
                    )
                )
        else:
            if source is None:
                source = document.revisions.first()
            content, file_error = _read_revision_content(source)
            editor_form = EditorForm(initial={"content": content})

        return self._render(
            request,
            "editor",
            {
                "title": _("Edit %(document)s") % {"document": document},
                "document": document,
                "editor_form": editor_form,
                "upload_form": upload_form,
                "file_error": file_error,
            },
        )

    @method_decorator(require_safe)
    def revision_list_view(
        self, request: HttpRequest, document_id: int
    ) -> TemplateResponse:
        self._require_permission(request)
        document = get_object_or_404(Document, pk=document_id)
        revisions = document.revisions.select_related("created_by").prefetch_related(
            "publications"
        )
        page = Paginator(revisions, self.list_per_page).get_page(request.GET.get("p"))
        return self._render(
            request,
            "history",
            {
                "title": _("Revision history"),
                "document": document,
                "page": page,
            },
        )

    @method_decorator(require_safe)
    def revision_view(
        self, request: HttpRequest, document_id: int, revision_id: int
    ) -> TemplateResponse:
        self._require_permission(request)
        revision = self._revision(document_id, revision_id)
        content, file_error = _read_revision_content(revision)
        return self._render(
            request,
            "revision",
            {
                "title": str(revision),
                "document": revision.document,
                "revision": revision,
                "content": content,
                "file_error": file_error,
                "publications": revision.publications.all(),
            },
        )

    @method_decorator(require_safe)
    def preview_view(
        self, request: HttpRequest, document_id: int, revision_id: int
    ) -> HttpResponse:
        self._require_permission(request)
        revision = self._revision(document_id, revision_id)
        try:
            content = revision.read_content()
        except RevisionFileError as error:
            return HttpResponseBadRequest(
                str(error), content_type="text/plain; charset=utf-8"
            )
        return HttpResponse(content, content_type="text/plain; charset=utf-8")


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    """Configure public file addresses and review and publish selected revisions."""

    list_display = ("__str__", "current_document", "revision")
    fields = ("directory", "filename", "current_document", "revision")
    readonly_fields = ("current_document", "revision")
    form = PublicationForm
    change_form_template = "admin/llms_manager/publication_change_form.html"

    class Media:
        """Declare the publication admin stylesheet."""

        css: ClassVar[dict[str, tuple[str, ...]]] = {"all": ("llms_manager/admin.css",)}

    def get_queryset(self, request: HttpRequest) -> QuerySet[Publication]:
        return super().get_queryset(request).select_related("revision__document")

    def get_fields(self, request, obj: Publication | None = None) -> tuple[str, ...]:
        if obj is None:
            return ("directory", "filename")
        return self.fields

    @admin.display(description=gettext_lazy("Document"))
    def current_document(self, obj: Publication | None) -> Document | None:
        return obj.revision.document if obj and obj.revision else None

    def save_model(self, request, obj: Publication, form, change: bool) -> None:
        obj.save(update_fields=("directory", "filename") if change else None)
        obj.publish(
            revision=form.cleaned_data["publish_revision"],
            expected_revision_id=form.cleaned_data["expected_revision_id"],
        )

    def construct_change_message(self, request, form, formsets, add=False):
        revision = form.cleaned_data["publish_revision"]
        return _("Published %(document)s, revision %(number)s.") % {
            "document": revision.document,
            "number": revision.number,
        }

    def response_change(self, request, obj):
        if "_popup" in request.POST:
            return super().response_change(request, obj)

        change_url = reverse(
            f"{self.admin_site.name}:llms_manager_publication_change",
            kwargs={"object_id": obj.pk},
        )
        self.message_user(request, _("Revision published."), messages.SUCCESS)
        return HttpResponseRedirect(
            add_preserved_filters(
                {
                    "preserved_filters": self.get_preserved_filters(request),
                    "opts": self.opts,
                },
                change_url,
            )
        )

    def _can_view_documents(self, request: HttpRequest) -> bool:
        return request.user.has_perm(
            "llms_manager.view_document"
        ) or request.user.has_perm("llms_manager.change_document")

    def _get_selection_context(
        self,
        request: HttpRequest,
        publication: Publication,
        can_view_documents: bool,
    ) -> tuple[bool, dict[str, Any]]:
        """Build document and revision selection forms and their current state."""
        published = candidate = publication.revision
        document_form = None
        revision_form = None
        selected_document = None
        selection_data = request.POST if request.method == "POST" else request.GET
        selecting = (
            bool(selection_data.get("document"))
            if request.method == "POST"
            else any(
                key in selection_data
                for key in ("document", "revision", "select_document", "show_diff")
            )
        )
        show_diff = "show_diff" in selection_data or (
            request.method == "POST" and bool(selection_data.get("revision"))
        )
        if selecting and not can_view_documents:
            raise PermissionDenied
        if can_view_documents:
            selected_document = published.document if published else None
            document_form = DocumentSelectionForm(
                data=selection_data if selecting else None,
                initial={"document": selected_document},
            )
            if selecting:
                candidate = None
                selected_document = (
                    document_form.cleaned_data["document"]
                    if document_form.is_valid()
                    else None
                )
            if selected_document is not None:
                initial_revision = (
                    published
                    if published and published.document_id == selected_document.pk
                    else None
                )
                revision_form = RevisionSelectionForm(
                    document=selected_document,
                    data=selection_data if show_diff else None,
                    initial={"revision": initial_revision},
                )
                if revision_form.is_bound and revision_form.is_valid():
                    candidate = revision_form.cleaned_data["revision"]
        return selecting, {
            "candidate": candidate,
            "document_form": document_form,
            "revision_form": revision_form,
            "selected_document": selected_document,
        }

    def _get_preview_context(
        self,
        request: HttpRequest,
        publication: Publication,
        candidate: Revision | None,
        can_view_documents: bool,
    ) -> dict[str, object]:
        """Build revision content, comparison, and the publication confirmation form."""
        published = publication.revision
        content, file_error = _read_revision_content(candidate)
        is_published = candidate is not None and candidate.pk == publication.revision_id
        diff = None
        diff_error = None
        line_endings_only = False
        if candidate and not is_published and not file_error:
            previous_content, previous_error = _read_revision_content(published)
            if previous_error:
                diff_error = _(
                    "The published revision file is corrupted or missing. "
                    "Comparison is unavailable."
                )
            else:
                diff = mark_safe(
                    HtmlDiff(tabsize=4, wrapcolumn=100)
                    .make_table(
                        previous_content.splitlines(),
                        content.splitlines(),
                        fromdesc=escape(
                            f"{published.document}: {published}"
                            if published
                            else _("No published revision")
                        ),
                        todesc=escape(f"{candidate.document}: {candidate}"),
                        context=True,
                        numlines=3,
                    )
                    .replace(
                        "<td>&nbsp;No Differences Found&nbsp;</td>",
                        format_html(
                            "<td>&nbsp;{}&nbsp;</td>", _("No differences found")
                        ),
                    )
                )
                line_endings_only = (
                    previous_content != content
                    and previous_content.splitlines() == content.splitlines()
                )

        publish_form = None
        if (
            candidate
            and not file_error
            and can_view_documents
            and (
                self.has_add_permission(request)
                if publication.pk is None
                else self.has_change_permission(request, obj=publication)
            )
        ):
            publish_form = PublishForm(
                auto_id="publication_%s",
                initial={
                    "reviewed_revision": candidate,
                    "expected_revision_id": publication.revision_id,
                },
            )
        return {
            "is_published": is_published,
            "content": content,
            "file_error": file_error,
            "diff": diff,
            "diff_error": diff_error,
            "line_endings_only": line_endings_only,
            "publish_form": publish_form,
        }

    def _redirect_to_review(
        self, request: HttpRequest, object_id: str | None
    ) -> HttpResponseRedirect:
        """Redirect to revision review with the submitted address and selection."""
        change_url = reverse(
            f"{self.admin_site.name}:llms_manager_publication_"
            f"{'add' if object_id is None else 'change'}",
            kwargs={} if object_id is None else {"object_id": object_id},
        )
        query = {
            field: request.POST[field]
            for field in ("directory", "filename", "document", "revision")
            if field in request.POST
        }
        query["show_diff"] = "1"
        return HttpResponseRedirect(f"{change_url}?{urlencode(query)}")

    @method_decorator(csrf_protect)
    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, object] | None = None,
    ) -> HttpResponse:
        if request.method not in ("GET", "POST"):
            return super().changeform_view(request, object_id, form_url, extra_context)

        can_view_documents = self._can_view_documents(request)
        if request.method == "POST" and not can_view_documents:
            raise PermissionDenied

        if object_id is None:
            if not self.has_add_permission(request):
                raise PermissionDenied
            publication = Publication()
        else:
            publication = self.get_object(request, object_id)
            if publication is None:
                raise Http404
            if not self.has_view_or_change_permission(request, obj=publication):
                raise PermissionDenied

        if request.method == "POST" and (
            "select_document" in request.POST or "show_diff" in request.POST
        ):
            query = {
                key: request.POST[key]
                for key in (
                    "directory",
                    "filename",
                    "document",
                    "revision",
                    "_popup",
                    "_to_field",
                )
                if key in request.POST
            }
            page_action = (
                "show_diff" if "show_diff" in request.POST else "select_document"
            )
            query[page_action] = "1"
            return HttpResponseRedirect(f"{request.path}?{urlencode(query=query)}")

        selecting, selection_context = self._get_selection_context(
            request, publication, can_view_documents
        )
        preview_context = self._get_preview_context(
            request, publication, selection_context["candidate"], can_view_documents
        )
        context = {
            "publication": publication,
            "published": publication.revision,
            **selection_context,
            **preview_context,
            "can_edit_document": request.user.has_perm("llms_manager.change_document"),
            **(extra_context or {}),
        }
        try:
            response = super().changeform_view(request, object_id, form_url, context)
        except (PublicationConflict, RevisionFileError) as error:
            self.message_user(request, str(error), messages.ERROR)
            return self._redirect_to_review(request, object_id)

        if (
            request.method == "GET"
            and selecting
            and isinstance(response, TemplateResponse)
        ):
            response.context_data["adminform"].form.initial.update(
                {
                    field: request.GET[field]
                    for field in ("directory", "filename")
                    if field in request.GET
                }
            )
        elif (
            request.method == "POST"
            and response.status_code == 302
            and "_continue" in request.POST
            and "_popup" not in request.POST
        ):
            url = urlsplit(response["Location"])
            parameters = dict(parse_qsl(url.query, keep_blank_values=True))
            for field in (
                "directory",
                "filename",
                "document",
                "revision",
                "select_document",
                "show_diff",
            ):
                parameters.pop(field, None)
            response["Location"] = urlunsplit(
                url._replace(query=urlencode(query=parameters))
            )
        return response
