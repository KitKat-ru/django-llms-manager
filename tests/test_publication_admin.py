from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from re import findall, search
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import urljoin

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.http import HttpResponse
from django.test import Client
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.translation import override

from llms_manager.models import Document, Publication, Revision
from tests.html import PublicationFormParser

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser
    from pytest_django import Settings


@pytest.mark.django_db
@pytest.mark.usefixtures("cache_backend")
class TestPublicationAdmin:
    """Check named publication configuration, staged review, and guarded publishing."""

    @pytest.fixture
    def document(self) -> Document:
        return Document.objects.create(name="Main document")

    @pytest.fixture
    def publication(self) -> Publication:
        return Publication.objects.create()

    @pytest.fixture
    def editor(self, django_user_model: type[AbstractUser]) -> AbstractUser:
        return django_user_model.objects.create_superuser(
            username="editor", email="editor@example.com", password=None
        )

    @pytest.fixture
    def editor_client(self, client: Client, editor: AbstractUser) -> Client:
        client.force_login(editor)
        return client

    @pytest.fixture
    def change_url(self, publication: Publication) -> str:
        return reverse(
            viewname="admin:llms_manager_publication_change",
            kwargs={"object_id": publication.pk},
        )

    @pytest.fixture(autouse=True)
    def english_language(self) -> Iterator[None]:
        with override("en"):
            yield

    @pytest.fixture(autouse=True)
    def media_root(self, settings: Settings, tmp_path: Path) -> Path:
        settings.MEDIA_ROOT = tmp_path
        return tmp_path

    @pytest.fixture
    def cache_backend(self, settings: Settings, tmp_path: Path) -> None:
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": str(tmp_path),
            }
        }

    @pytest.fixture(params=["admin", "content_admin"])
    def admin_namespace(
        self, request: pytest.FixtureRequest, settings: Settings
    ) -> str:
        if request.param == "content_admin":
            settings.ROOT_URLCONF = "tests.custom_admin_urls"
        return request.param

    @pytest.fixture
    def positional_reverse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Apply a reverse wrapper that requires a positional view name."""

        def reverse_with_positional_name(*args: Any, **kwargs: Any) -> str:
            """Read the view name positionally before reversing the URL."""
            return reverse(args[0], *args[1:], **kwargs)

        monkeypatch.setattr("llms_manager.admin.reverse", reverse_with_positional_name)
        monkeypatch.setattr("llms_manager.models.reverse", reverse_with_positional_name)

    @pytest.fixture(params=["missing", "invalid_utf8"])
    def damage_file(self, request: pytest.FixtureRequest) -> Callable[[Revision], None]:
        def damage(revision: Revision) -> None:
            if request.param == "missing":
                revision.file.storage.delete(revision.file.name)
            else:
                Path(revision.file.path).write_bytes(b"\xff\xfe")

        return damage

    def _review_data(self, revision: Revision) -> dict[str, int | str]:
        return {
            "document": revision.document_id,
            "revision": revision.pk,
            "show_diff": "1",
        }

    def _publish_data(
        self,
        revision: Revision,
        expected_revision_id: int | None,
        *,
        directory: str = "/",
        filename: str = Publication.Filename.LLMS,
    ) -> dict[str, int | str]:
        return {
            "directory": directory,
            "filename": filename,
            "document": revision.document_id,
            "revision": revision.pk,
            "reviewed_revision": revision.pk,
            "expected_revision_id": (
                expected_revision_id if expected_revision_id is not None else ""
            ),
            "_publish": "1",
            "_continue": "1",
        }

    def _submit_publication_form(
        self,
        client: Client,
        page: HttpResponse,
        button: str,
        **edits: int | str,
    ) -> HttpResponse:
        """Submit rendered publication fields with a selected button and edits."""
        form = PublicationFormParser(button=button)
        form.feed(page.content.decode())
        assert form.form_found
        assert form.method == "post"
        assert form.button_found
        form.fields.update({name: str(value) for name, value in edits.items()})
        url = urljoin(page.wsgi_request.get_full_path(), form.action)
        return client.post(url, form.fields, follow=True)

    @pytest.mark.parametrize(
        ("language", "message"),
        [
            ("en", "The revision file is corrupted or missing."),
            ("ru", "Файл версии повреждён или отсутствует."),
        ],
    )
    def test_unreadable_published_revision_keeps_its_card_accessible(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        damage_file: Callable[[Revision], None],
        language: str,
        message: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        publication.publish(revision=current, expected_revision_id=None)
        damage_file(current)

        with override(language):
            response = editor_client.get(change_url)

        assert response.status_code == 200
        assert message.encode() in response.content
        assert response.context["candidate"].pk == current.pk
        assert response.context["is_published"]
        assert response.context["publish_form"] is None
        publish_button = search(
            r'<input\b[^>]*name="_publish"[^>]*>', response.content.decode()
        )
        assert publish_button is not None
        assert " disabled" in publish_button.group()
        assert b'<pre class="llms-content">' not in response.content
        assert response.context["document_form"] is not None
        assert not LogEntry.objects.exists()

    @pytest.mark.parametrize("creating", [False, True])
    def test_unreadable_selected_revision_cannot_create_or_move_a_publication(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        damage_file: Callable[[Revision], None],
        creating: bool,
    ) -> None:
        publication = None if creating else Publication.objects.create()
        current = None
        if publication is not None:
            current = Revision.create_from_content(document, "# Current\n", editor)
            publication.publish(revision=current, expected_revision_id=None)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        url = reverse(
            "admin:llms_manager_publication_add"
            if creating
            else "admin:llms_manager_publication_change",
            kwargs={} if creating else {"object_id": publication.pk},
        )
        review = editor_client.get(url, self._review_data(revision=candidate))
        assert review.status_code == 200
        assert review.context["publish_form"] is not None
        damage_file(candidate)

        unavailable = editor_client.get(url, self._review_data(revision=candidate))
        response = editor_client.post(
            url,
            self._publish_data(
                revision=candidate,
                expected_revision_id=current.pk if current else None,
                directory="/courses/",
                filename="llms-full.txt",
            ),
        )

        assert unavailable.status_code == 200
        assert unavailable.context["publish_form"] is None
        assert b"The revision file is corrupted or missing." in unavailable.content
        assert response.status_code == 200
        form = response.context["adminform"].form
        assert set(form.errors) == {"__all__"}
        assert list(form.non_field_errors()) == [
            "The revision file is corrupted or missing."
        ]
        assert form["directory"].value() == "/courses/"
        assert form["filename"].value() == "llms-full.txt"
        assert not LogEntry.objects.exists()
        if creating:
            assert not Publication.objects.exists()
        else:
            publication.refresh_from_db()
            assert publication.directory == "/"
            assert publication.filename == "llms.txt"
            assert publication.revision_id == current.pk
            assert editor_client.get("/llms.txt").content == b"# Current\n"
        assert editor_client.get("/courses/llms-full.txt").status_code == 404

    def test_readable_revision_can_replace_an_unreadable_published_file(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        damage_file: Callable[[Revision], None],
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        publication.publish(revision=current, expected_revision_id=None)
        damage_file(current)

        review = editor_client.get(change_url, self._review_data(revision=candidate))

        assert review.status_code == 200
        assert review.context["diff"] is None
        assert b"Comparison is unavailable." in review.content
        assert b'class="llms-diff"' not in review.content
        assert b"# Candidate" in review.content
        assert review.context["publish_form"] is not None

        response = editor_client.post(
            change_url,
            self._publish_data(revision=candidate, expected_revision_id=current.pk),
        )

        assert response.status_code == 302
        publication.refresh_from_db()
        assert publication.revision_id == candidate.pk
        assert editor_client.get("/llms.txt").content == b"# Candidate\n"
        assert LogEntry.objects.count() == 1

    @pytest.mark.parametrize("creating", [False, True])
    def test_file_loss_after_validation_rolls_back_publication_changes(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        creating: bool,
    ) -> None:
        publication = None if creating else Publication.objects.create()
        current = None
        if publication is not None:
            current = Revision.create_from_content(document, "# Current\n", editor)
            publication.publish(revision=current, expected_revision_id=None)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        url = reverse(
            "admin:llms_manager_publication_add"
            if creating
            else "admin:llms_manager_publication_change",
            kwargs={} if creating else {"object_id": publication.pk},
        )
        publish = Publication.publish

        def remove_file_and_publish(
            instance: Publication,
            *,
            revision: Revision,
            expected_revision_id: int | None,
        ) -> None:
            revision.file.storage.delete(revision.file.name)
            publish(
                instance, revision=revision, expected_revision_id=expected_revision_id
            )

        with patch.object(Publication, "publish", new=remove_file_and_publish):
            response = editor_client.post(
                url,
                self._publish_data(
                    revision=candidate,
                    expected_revision_id=current.pk if current else None,
                    directory="/courses/",
                    filename="llms-full.txt",
                ),
                follow=True,
            )

        assert response.status_code == 200
        assert len(response.redirect_chain) == 1
        assert b"The revision file is corrupted or missing." in response.content
        assert response.context["candidate"].pk == candidate.pk
        assert response.context["publish_form"] is None
        form = response.context["adminform"].form
        assert form["directory"].value() == "/courses/"
        assert form["filename"].value() == "llms-full.txt"
        assert not LogEntry.objects.exists()
        if creating:
            assert not Publication.objects.exists()
        else:
            publication.refresh_from_db()
            assert publication.directory == "/"
            assert publication.filename == "llms.txt"
            assert publication.revision_id == current.pk

    def test_address_can_be_created_with_a_filename_choice(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)
        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            self._publish_data(
                revision=revision,
                expected_revision_id=None,
                directory="courses/python",
                filename="llms-full.txt",
            ),
        )

        publication = Publication.objects.get()
        assert response.status_code == 302
        assert response.url == reverse(
            viewname="admin:llms_manager_publication_change",
            kwargs={"object_id": publication.pk},
        )
        assert publication.directory == "/courses/python/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id == revision.pk
        assert editor_client.get(response.url).status_code == 200
        assert (
            editor_client.get(publication.get_absolute_url()).content == b"# Selected\n"
        )

    @pytest.mark.parametrize("missing", ["document", "revision", "reviewed_revision"])
    def test_new_publication_requires_a_reviewed_revision(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        missing: str,
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)
        data = self._publish_data(revision=revision, expected_revision_id=None)
        data.pop(missing)

        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"), data
        )

        assert response.status_code == 200
        assert response.context["adminform"].form.non_field_errors()
        assert not Publication.objects.exists()
        assert not LogEntry.objects.exists()

    def test_new_publication_shows_document_selection(
        self, document: Document, editor_client: Client
    ) -> None:
        response = editor_client.get(
            reverse(viewname="admin:llms_manager_publication_add")
        )

        assert response.status_code == 200
        assert response.context["add"]
        assert response.context["publication"].pk is None
        document_field = response.context["document_form"].fields["document"]
        assert list(document_field.queryset) == [document]
        html = response.content.decode()
        form_start = html.index('id="publication_form"')
        assert (
            form_start
            < html.index('name="document"')
            < html.index("</form>", form_start)
        )
        assert b'name="select_document"' in response.content
        assert b'value="Save"' not in response.content
        assert b'name="_publish"' in response.content
        assert not Publication.objects.exists()

    @pytest.mark.parametrize("existing", [False, True])
    def test_selection_and_review_preserve_the_unsaved_address(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        existing: bool,
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)
        publication = Publication.objects.create() if existing else None
        url = (
            reverse(
                viewname="admin:llms_manager_publication_change",
                kwargs={"object_id": publication.pk},
            )
            if publication
            else reverse(viewname="admin:llms_manager_publication_add")
        )
        selection = {
            "directory": "courses/python",
            "filename": "llms-full.txt",
            "document": document.pk,
            "csrfmiddlewaretoken": "not-for-urls",
        }

        selecting = editor_client.post(
            url, {**selection, "select_document": "1"}, follow=True
        )

        assert selecting.status_code == 200
        revision_field = selecting.context["revision_form"].fields["revision"]
        assert list(revision_field.queryset) == [revision]

        reviewing = editor_client.post(
            url,
            {**selection, "revision": revision.pk, "show_diff": "1"},
            follow=True,
        )

        assert reviewing.status_code == 200
        assert reviewing.context["candidate"].pk == revision.pk
        assert reviewing.context["content"] == "# Selected\n"
        assert reviewing.context["diff"]
        assert reviewing.context["publish_form"] is not None
        form = reviewing.context["adminform"].form
        assert form["directory"].value() == "courses/python"
        assert form["filename"].value() == "llms-full.txt"
        assert all(
            "csrfmiddlewaretoken" not in url
            for url, _status in selecting.redirect_chain + reviewing.redirect_chain
        )
        assert Publication.objects.count() == int(existing)
        if publication:
            publication.refresh_from_db()
            assert publication.directory == "/"
            assert publication.filename == "llms.txt"
            assert publication.revision_id is None
        assert not LogEntry.objects.exists()

    @pytest.mark.parametrize("existing", [False, True])
    def test_publication_is_saved_using_rendered_form_fields(
        self,
        document: Document,
        editor: AbstractUser,
        admin_namespace: str,
        positional_reverse: None,
        existing: bool,
    ) -> None:
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(editor)
        selected_revision = Revision.create_from_content(
            document, "# Selected\n", editor
        )
        Revision.create_from_content(document, "# Newer draft\n", editor)
        publication = None
        if existing:
            previous_document = Document.objects.create(name="Previous document")
            previous = Revision.create_from_content(
                previous_document, "# Previous\n", editor
            )
            publication = Publication.objects.create()
            publication.publish(revision=previous, expected_revision_id=None)
        url = reverse(
            f"{admin_namespace}:llms_manager_publication_change"
            if existing
            else f"{admin_namespace}:llms_manager_publication_add",
            kwargs={"object_id": publication.pk} if existing else {},
        )
        initial = browser.get(url)
        assert initial.status_code == 200

        selecting = self._submit_publication_form(
            browser,
            initial,
            "select_document",
            directory="courses/python",
            filename="llms-full.txt",
            document=document.pk,
        )
        assert selecting.status_code == 200
        reviewing = self._submit_publication_form(
            browser, selecting, "show_diff", revision=selected_revision.pk
        )

        assert reviewing.status_code == 200
        assert reviewing.context["candidate"].pk == selected_revision.pk
        assert Publication.objects.count() == int(existing)
        assert not LogEntry.objects.exists()
        if publication is not None:
            publication.refresh_from_db()
            assert publication.directory == "/"
            assert publication.filename == "llms.txt"
            assert publication.revision_id == previous.pk

        response = self._submit_publication_form(browser, reviewing, "_publish")

        publication = Publication.objects.get()
        assert response.status_code == 200
        assert response.wsgi_request.path == reverse(
            f"{admin_namespace}:llms_manager_publication_change",
            kwargs={"object_id": publication.pk},
        )
        assert not response.wsgi_request.GET
        assert publication.directory == "/courses/python/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id == selected_revision.pk
        assert response.context["selected_document"].pk == document.pk
        assert response.context["candidate"].pk == selected_revision.pk
        assert response.context["is_published"]
        assert browser.get("/llms.txt").status_code == 404
        assert browser.get(publication.get_absolute_url()).content == b"# Selected\n"
        log = LogEntry.objects.get()
        assert log.user_id == editor.pk
        assert log.object_id == str(publication.pk)

    @pytest.mark.parametrize("use_current_revision", [False, True])
    def test_save_and_publish_updates_the_address_and_revision_together(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        use_current_revision: bool,
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        candidate = (
            current
            if use_current_revision
            else Revision.create_from_content(document, "# New\n", editor)
        )
        publication.publish(revision=current, expected_revision_id=None)
        review = editor_client.get(change_url, self._review_data(revision=candidate))
        assert review.context["publish_form"] is not None

        response = editor_client.post(
            review.wsgi_request.get_full_path(),
            self._publish_data(
                revision=candidate,
                expected_revision_id=current.pk,
                directory="courses/python",
                filename="llms-full.txt",
            ),
            follow=True,
        )

        assert response.status_code == 200
        publication.refresh_from_db()
        assert publication.directory == "/courses/python/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id == candidate.pk
        assert editor_client.get("/llms.txt").status_code == 404
        public_response = editor_client.get(publication.get_absolute_url())
        assert public_response.content.decode() == candidate.read_content()

    @pytest.mark.parametrize("invalid_address", [True, False])
    def test_invalid_or_occupied_address_does_not_publish(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        invalid_address: bool,
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        publication.publish(revision=current, expected_revision_id=None)
        if not invalid_address:
            Publication.objects.create(directory="/courses/")

        response = editor_client.post(
            change_url,
            self._publish_data(
                revision=candidate,
                expected_revision_id=current.pk,
                directory="/../" if invalid_address else "/courses/",
            ),
        )

        assert response.status_code == 200
        assert response.context["adminform"].form.errors
        publication.refresh_from_db()
        assert publication.directory == "/"
        assert publication.filename == "llms.txt"
        assert publication.revision_id == current.pk
        assert not LogEntry.objects.exists()

    @pytest.mark.parametrize("different_document", [False, True])
    def test_changed_selection_requires_another_review(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        different_document: bool,
    ) -> None:
        reviewed = Revision.create_from_content(document, "# Reviewed\n", editor)
        other = Document.objects.create(name="Other document")
        selected = Revision.create_from_content(
            other if different_document else document, "# Selected\n", editor
        )
        data = self._publish_data(revision=reviewed, expected_revision_id=None)
        data["document"] = selected.document_id
        data["revision"] = selected.pk

        response = editor_client.post(change_url, data)

        assert response.status_code == 200
        assert response.context["adminform"].form.non_field_errors()
        publication.refresh_from_db()
        assert publication.revision_id is None
        assert not LogEntry.objects.exists()

    def test_invalid_new_address_keeps_the_selected_revision(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)

        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            self._publish_data(
                revision=revision,
                expected_revision_id=None,
                directory="/courses/",
                filename="other.txt",
            ),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"filename"}
        assert response.context["selected_document"].pk == document.pk
        assert response.context["candidate"].pk == revision.pk
        assert not Publication.objects.exists()

    def test_the_only_save_action_requires_review_before_publishing(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        Revision.create_from_content(document, "# Draft\n", editor)

        response = editor_client.get(
            change_url, {"document": document.pk, "select_document": "1"}
        )

        assert response.status_code == 200
        html = response.content.decode()
        save_button = search(r'<input\b[^>]*value="Save"[^>]*>', html)
        publish_button = search(r'<input\b[^>]*name="_publish"[^>]*>', html)
        assert save_button is None
        assert publish_button is not None
        assert 'form="publication_form"' in publish_button.group()
        assert " disabled" in publish_button.group()
        assert publish_button.start() < html.index('id="publication_form"')
        assert len(findall(r'<input\b[^>]*form="publication_form"[^>]*>', html)) == 1
        assert 'id="llms-publication-publish-form"' not in html

    def test_other_filenames_are_rejected(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)

        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            self._publish_data(
                revision=revision,
                expected_revision_id=None,
                filename="other.txt",
            ),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"filename"}
        assert not Publication.objects.exists()

    def test_publication_list_shows_each_independent_revision(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
    ) -> None:
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        revision = Revision.create_from_content(document, "# Full\n", editor)
        full.publish(revision=revision, expected_revision_id=None)

        response = editor_client.get(
            reverse(viewname="admin:llms_manager_publication_changelist")
        )

        assert response.status_code == 200
        rows = {row.pk: row.revision_id for row in response.context["cl"].queryset}
        assert rows == {publication.pk: None, full.pk: revision.pk}

    def test_document_selection_filters_revisions_without_publishing(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        other = Document.objects.create(name="Other document")
        Revision.create_from_content(other, "# Other\n", editor)
        selected = Revision.create_from_content(document, "# Own\n", editor)

        response = editor_client.get(
            change_url, {"document": document.pk, "select_document": "1"}
        )

        assert response.status_code == 200
        revision_field = response.context["revision_form"].fields["revision"]
        assert list(revision_field.queryset) == [selected]
        assert (str(selected.pk), "Revision 1") in [
            (str(value), label) for value, label in revision_field.choices
        ]
        assert response.context["candidate"] is None
        publication.refresh_from_db()
        assert publication.revision_id is None

    def test_revision_from_another_document_cannot_be_reviewed(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        other = Document.objects.create(name="Other document")
        revision = Revision.create_from_content(other, "# Private\n", editor)

        response = editor_client.get(
            change_url,
            {"document": document.pk, "revision": revision.pk, "show_diff": "1"},
        )

        assert response.status_code == 200
        assert "revision" in response.context["revision_form"].errors
        assert response.context["candidate"] is None
        assert b"# Private" not in response.content
        publication.refresh_from_db()
        assert publication.revision_id is None

    def test_review_compares_only_with_the_selected_publication(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        other = Document.objects.create(name="Other <site>")
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        current = Revision.create_from_content(other, "RootBaseline\n", editor)
        full_revision = Revision.create_from_content(other, "FullBaseline\n", editor)
        candidate = Revision.create_from_content(document, "CandidateContent\n", editor)
        publication.publish(revision=current, expected_revision_id=None)
        full.publish(revision=full_revision, expected_revision_id=None)

        response = editor_client.get(change_url, self._review_data(revision=candidate))

        assert response.status_code == 200
        assert response.context["published"].pk == current.pk
        assert (
            response.context["publish_form"]["expected_revision_id"].value()
            == current.pk
        )
        diff = str(response.context["diff"])
        assert "RootBaseline" in strip_tags(diff)
        assert "CandidateContent" in strip_tags(diff)
        assert "FullBaseline" not in strip_tags(diff)
        assert "Other &lt;site&gt;" in diff
        assert "Other <site>" not in diff
        html = response.content.decode()
        element_ids = findall(r'\sid="([^"]+)"', html)
        assert len(element_ids) == len(set(element_ids))
        publish_button = search(r'<input\b[^>]*name="_publish"[^>]*>', html)
        assert publish_button is not None
        assert " disabled" not in publish_button.group()
        assert 'form="publication_form"' in publish_button.group()
        assert publish_button.start() < html.index('id="publication_form"')
        assert publish_button.start() < html.index('class="llms-diff"')
        form_start = html.index('id="publication_form"')
        assert (
            form_start
            < html.index('name="reviewed_revision"')
            < html.index("</form>", form_start)
        )
        publication.refresh_from_db()
        assert publication.revision_id == current.pk

    def test_publication_selects_any_revision_and_records_an_admin_log(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        other = Document.objects.create(name="Other document")
        Revision.create_from_content(other, "# Other\n", editor)
        selected = Revision.create_from_content(document, "# Selected\n", editor)
        Revision.create_from_content(document, "# Latest\n", editor)

        response = editor_client.post(
            change_url,
            self._publish_data(revision=selected, expected_revision_id=None),
        )

        assert response.status_code == 302
        assert response.url == change_url
        publication.refresh_from_db()
        assert publication.revision_id == selected.pk
        assert (
            editor_client.get(publication.get_absolute_url()).content == b"# Selected\n"
        )
        review = editor_client.get(response.url)
        assert review.status_code == 200
        assert b'class="viewsitelink"' not in review.content
        assert b"This revision is currently published." in review.content
        assert b"Ready to publish:" not in review.content
        assert b'class="llms-diff"' not in review.content
        assert b"<details open>" in review.content
        assert f'href="{publication.get_absolute_url()}"'.encode() in review.content
        log = LogEntry.objects.get()
        assert log.content_type.model == "publication"
        assert log.object_id == str(publication.pk)
        assert document.name in log.change_message
        assert f"revision {selected.number}." in log.change_message

    @pytest.mark.parametrize("content", ["# Same\n", ""])
    @pytest.mark.parametrize(
        ("language", "message"),
        [("en", "No differences found"), ("ru", "Различий нет")],
    )
    def test_another_revision_with_identical_content_still_shows_diff(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        content: str,
        language: str,
        message: str,
    ) -> None:
        current = Revision.create_from_content(document, content, editor)
        candidate = Revision.create_from_content(document, content, editor)
        publication.publish(revision=current, expected_revision_id=None)

        with override(language):
            response = editor_client.get(
                change_url, self._review_data(revision=candidate)
            )

        assert response.status_code == 200
        assert b'class="llms-diff"' in response.content
        assert not response.context["is_published"]
        assert message in strip_tags(str(response.context["diff"]))
        assert b"No Differences Found" not in response.content

    def test_diff_translation_preserves_document_names(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        document.name = "No Differences Found"
        document.save()
        current = Revision.create_from_content(document, "# Before\n", editor)
        candidate = Revision.create_from_content(document, "# After\n", editor)
        publication.publish(revision=current, expected_revision_id=None)

        with override("ru"):
            response = editor_client.get(
                change_url, self._review_data(revision=candidate)
            )

        assert response.status_code == 200
        assert document.name in strip_tags(str(response.context["diff"]))
        assert "Различий нет" not in strip_tags(str(response.context["diff"]))

    @pytest.mark.parametrize("candidate_content", ["# Content", "# Content\r\n"])
    @pytest.mark.parametrize(
        ("language", "message"),
        [
            ("en", "The text is unchanged. Only line breaks differ."),
            ("ru", "Текст совпадает. Отличаются только переносы строк."),
        ],
    )
    def test_line_break_differences_are_reported_in_the_active_language(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        candidate_content: str,
        language: str,
        message: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Content\n", editor)
        candidate = Revision.create_from_content(document, candidate_content, editor)
        publication.publish(revision=current, expected_revision_id=None)

        with override(language):
            response = editor_client.get(
                change_url, self._review_data(revision=candidate)
            )

        assert response.status_code == 200
        assert message.encode() in response.content
        assert response.context["line_endings_only"]
        assert b'class="llms-diff"' not in response.content

    def test_stale_publication_can_be_reviewed_and_retried(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        admin_namespace: str,
        positional_reverse: None,
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        replacement = Revision.create_from_content(document, "# Replacement\n", editor)
        publication.publish(revision=first, expected_revision_id=None)
        change_url = reverse(
            f"{admin_namespace}:llms_manager_publication_change",
            kwargs={"object_id": publication.pk},
        )
        review = editor_client.get(
            change_url,
            {
                **self._review_data(revision=candidate),
                "directory": "/courses/",
                "filename": "llms-full.txt",
            },
        )
        assert review.status_code == 200
        publication.publish(revision=replacement, expected_revision_id=first.pk)

        response = self._submit_publication_form(editor_client, review, "_publish")

        assert response.status_code == 200
        assert b"The published revision changed." in response.content
        assert response.wsgi_request.path == change_url
        assert response.context["candidate"].pk == candidate.pk
        assert response.context["published"].pk == replacement.pk
        diff = strip_tags(str(response.context["diff"]))
        assert "Replacement" in diff
        assert "Candidate" in diff
        publication.refresh_from_db()
        assert publication.directory == "/"
        assert publication.filename == "llms.txt"
        assert publication.revision_id == replacement.pk
        assert response.context["adminform"].form["directory"].value() == "/courses/"
        assert response.context["adminform"].form["filename"].value() == "llms-full.txt"
        assert not LogEntry.objects.exists()

        retried = self._submit_publication_form(editor_client, response, "_publish")

        assert retried.status_code == 200
        assert retried.wsgi_request.path == change_url
        assert not retried.wsgi_request.GET
        assert retried.context["is_published"]
        publication.refresh_from_db()
        assert publication.directory == "/courses/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id == candidate.pk
        assert editor_client.get("/llms.txt").status_code == 404
        assert (
            editor_client.get(publication.get_absolute_url()).content
            == b"# Candidate\n"
        )
        log = LogEntry.objects.get()
        assert log.user_id == editor.pk
        assert log.object_id == str(publication.pk)

    def test_other_addresses_do_not_invalidate_the_review(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        first = Revision.create_from_content(document, "# First\n", editor)
        second = Revision.create_from_content(document, "# Second\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        publication.publish(revision=first, expected_revision_id=None)
        full.publish(revision=second, expected_revision_id=None)
        full.publish(revision=candidate, expected_revision_id=second.pk)

        response = editor_client.post(
            change_url,
            self._publish_data(revision=second, expected_revision_id=first.pk),
        )

        assert response.status_code == 302
        publication.refresh_from_db()
        full.refresh_from_db()
        assert publication.revision_id == second.pk
        assert full.revision_id == candidate.pk

    @pytest.mark.parametrize("already_published", [False, True])
    @pytest.mark.parametrize("keep_selection", [False, True])
    def test_address_cannot_be_saved_without_a_reviewed_revision(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        already_published: bool,
        keep_selection: bool,
    ) -> None:
        current = (
            Revision.create_from_content(document, "# Published\n", editor)
            if already_published
            else None
        )
        if current:
            publication.publish(revision=current, expected_revision_id=None)
        revision = Revision.create_from_content(document, "# Draft\n", editor)

        response = editor_client.post(
            change_url,
            {
                "directory": "/courses/",
                "filename": "llms-full.txt",
                "document": document.pk if keep_selection else "",
                "revision": revision.pk if keep_selection else "",
                "_continue": "1",
            },
            follow=True,
        )

        assert response.status_code == 200
        assert response.context["adminform"].form.non_field_errors()
        assert response.context["adminform"].form["directory"].value() == "/courses/"
        assert response.context["adminform"].form["filename"].value() == "llms-full.txt"
        publication.refresh_from_db()
        expected_revision_id = current.pk if current else None
        assert publication.directory == "/"
        assert publication.filename == "llms.txt"
        assert publication.revision_id == expected_revision_id
        assert editor_client.get("/courses/llms-full.txt").status_code == 404
        expected_content = b"# Published\n" if current else b""
        assert (
            editor_client.get(publication.get_absolute_url()).content
            == expected_content
        )
        assert not LogEntry.objects.exists()

    def test_saving_without_the_publish_marker_still_publishes_the_reviewed_revision(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        publication.publish(revision=current, expected_revision_id=None)
        data = self._publish_data(
            revision=candidate,
            expected_revision_id=current.pk,
            directory="/courses/",
            filename="llms-full.txt",
        )
        data.pop("_publish")

        response = editor_client.post(change_url, data)

        assert response.status_code == 302
        assert response.url == change_url
        publication.refresh_from_db()
        assert publication.directory == "/courses/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id == candidate.pk
        assert publication.get_absolute_url() == "/courses/llms-full.txt"
        assert editor_client.get("/llms.txt").status_code == 404
        assert (
            editor_client.get(publication.get_absolute_url()).content
            == b"# Candidate\n"
        )

    def test_view_permissions_allow_review_but_not_publication(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        client: Client,
        django_user_model: type[AbstractUser],
        change_url: str,
    ) -> None:
        viewer = django_user_model.objects.create_user(username="viewer", is_staff=True)
        viewer.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="llms_manager",
                codename__in=["view_publication", "view_document"],
            )
        )
        revision = Revision.create_from_content(document, "# Draft\n", editor)
        client.force_login(viewer)

        review = client.post(
            change_url, self._review_data(revision=revision), follow=True
        )
        adding = client.get(reverse(viewname="admin:llms_manager_publication_add"))
        publishing = client.post(
            change_url,
            self._publish_data(revision=revision, expected_revision_id=None),
        )

        assert review.status_code == 200
        assert review.context["candidate"].pk == revision.pk
        assert review.context["publish_form"] is None
        assert b'value="Save"' not in review.content
        assert b'name="_publish"' not in review.content
        assert adding.status_code == 403
        assert publishing.status_code == 403
        publication.refresh_from_db()
        assert publication.revision_id is None

    @pytest.mark.parametrize("already_published", [False, True])
    def test_document_permission_is_required_to_select_and_publish_revisions(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        client: Client,
        django_user_model: type[AbstractUser],
        change_url: str,
        already_published: bool,
    ) -> None:
        publisher = django_user_model.objects.create_user(
            username="publisher", is_staff=True
        )
        publisher.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="llms_manager",
                codename__in=["add_publication", "change_publication"],
            )
        )
        revision = Revision.create_from_content(document, "# Private\n", editor)
        if already_published:
            publication.publish(revision=revision, expected_revision_id=None)
        expected_revision_id = publication.revision_id
        client.force_login(publisher)

        card = client.get(change_url)
        review = client.get(change_url, self._review_data(revision=revision))
        new_review = client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {"document": document.pk, "revision": revision.pk, "show_diff": "1"},
        )
        publishing = client.post(
            change_url,
            self._publish_data(
                revision=revision, expected_revision_id=expected_revision_id
            ),
        )

        assert card.status_code == 200
        assert card.context["document_form"] is None
        assert card.context["publish_form"] is None
        publish_button = search(
            r'<input\b[^>]*name="_publish"[^>]*>', card.content.decode()
        )
        assert publish_button is not None
        assert " disabled" in publish_button.group()
        assert review.status_code == 403
        assert new_review.status_code == 403
        assert publishing.status_code == 403
        publication.refresh_from_db()
        assert publication.revision_id == expected_revision_id
        assert not LogEntry.objects.exists()

    @pytest.mark.parametrize("creating", [False, True])
    @pytest.mark.parametrize(
        "document_permission", ["view_document", "change_document"]
    )
    def test_staff_with_only_required_permissions_can_publish(
        self,
        document: Document,
        editor: AbstractUser,
        client: Client,
        django_user_model: type[AbstractUser],
        creating: bool,
        document_permission: str,
    ) -> None:
        publisher = django_user_model.objects.create_user(
            username="publisher", is_staff=True
        )
        publisher.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="llms_manager",
                codename__in=[
                    "add_publication" if creating else "change_publication",
                    document_permission,
                ],
            )
        )
        publication = None if creating else Publication.objects.create()
        current = None
        if publication is not None:
            current = Revision.create_from_content(document, "# Current\n", editor)
            publication.publish(revision=current, expected_revision_id=None)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        url = reverse(
            "admin:llms_manager_publication_add"
            if creating
            else "admin:llms_manager_publication_change",
            kwargs={} if creating else {"object_id": publication.pk},
        )
        client.force_login(publisher)

        review = client.get(url, self._review_data(revision=candidate))
        response = client.post(
            url,
            self._publish_data(
                revision=candidate,
                expected_revision_id=current.pk if current else None,
            ),
        )

        assert review.status_code == 200
        assert review.context["publish_form"] is not None
        assert response.status_code == 302
        publication = Publication.objects.get()
        assert publication.revision_id == candidate.pk
        assert client.get(publication.get_absolute_url()).content == b"# Candidate\n"
        assert LogEntry.objects.get().user_id == publisher.pk

    def test_new_publication_review_requires_csrf(
        self, document: Document, editor: AbstractUser
    ) -> None:
        revision = Revision.create_from_content(document, "# Private\n", editor)
        client = Client(enforce_csrf_checks=True)
        client.force_login(editor)

        response = client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {"document": document.pk, "revision": revision.pk, "show_diff": "1"},
        )

        assert response.status_code == 403
        assert not Publication.objects.exists()

    def test_publication_requires_login(self, client: Client, change_url: str) -> None:
        response = client.post(change_url)

        assert response.status_code == 302
        assert response.url.startswith(reverse(viewname="admin:login"))

    def test_publication_requires_csrf(
        self, document: Document, editor: AbstractUser, change_url: str
    ) -> None:
        revision = Revision.create_from_content(document, "# Draft\n", editor)
        client = Client(enforce_csrf_checks=True)
        client.force_login(editor)

        response = client.post(
            change_url,
            self._publish_data(revision=revision, expected_revision_id=None),
        )

        assert response.status_code == 403

    def test_admin_log_failure_rolls_back_publication(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        publication.publish(revision=current, expected_revision_id=None)

        with (
            patch(
                "llms_manager.admin.PublicationAdmin.log_change",
                side_effect=RuntimeError("Admin log failed"),
            ),
            pytest.raises(RuntimeError, match="Admin log failed"),
        ):
            editor_client.post(
                change_url,
                self._publish_data(
                    revision=candidate,
                    expected_revision_id=current.pk,
                    directory="/courses/",
                    filename="llms-full.txt",
                ),
            )

        publication.refresh_from_db()
        assert publication.directory == "/"
        assert publication.filename == "llms.txt"
        assert publication.revision_id == current.pk

    def test_admin_log_failure_rolls_back_new_publication(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)

        with (
            patch(
                "llms_manager.admin.PublicationAdmin.log_addition",
                side_effect=RuntimeError("Admin log failed"),
            ),
            pytest.raises(RuntimeError, match="Admin log failed"),
        ):
            editor_client.post(
                reverse(viewname="admin:llms_manager_publication_add"),
                self._publish_data(revision=revision, expected_revision_id=None),
            )

        assert not Publication.objects.exists()
        assert not LogEntry.objects.exists()

    def test_edit_as_new_revision_uses_the_selected_admin_namespace(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        admin_namespace: str,
        positional_reverse: None,
    ) -> None:
        source = Revision.create_from_content(document, "# Source\n", editor)
        publication.publish(revision=source, expected_revision_id=None)
        card = editor_client.get(
            reverse(
                f"{admin_namespace}:llms_manager_publication_change",
                kwargs={"object_id": publication.pk},
            )
        )
        editor_url = reverse(
            f"{admin_namespace}:llms_manager_document_edit_revision",
            kwargs={"document_id": document.pk, "revision_id": source.pk},
        )

        assert card.status_code == 200
        assert f'href="{editor_url}"'.encode() in card.content
        editing = editor_client.get(editor_url)
        assert editing.status_code == 200
        assert editing.context["editor_form"]["content"].value() == "# Source\n"

        response = editor_client.post(
            editor_url, {"save_revision": "1", "content": "# New revision\n"}
        )

        revision = document.revisions.first()
        assert revision.pk != source.pk
        assert response.status_code == 302
        assert response.url == reverse(
            f"{admin_namespace}:llms_manager_document_revision",
            kwargs={"document_id": document.pk, "revision_id": revision.pk},
        )
        saved = editor_client.get(response.url)
        assert saved.status_code == 200
        assert b"# New revision" in saved.content
        assert revision.read_content() == "# New revision\n"
        assert revision.created_by_id == editor.pk
        publication.refresh_from_db()
        assert publication.revision_id == source.pk

    @pytest.mark.parametrize(
        ("language", "document_names", "publication_names", "publish_label"),
        [
            (
                "en",
                ("document", "documents"),
                ("publication", "publications"),
                "Save and publish",
            ),
            (
                "ru",
                ("документ", "документы"),
                ("публикация", "публикации"),
                "Сохранить и опубликовать",
            ),
        ],
    )
    def test_model_names_and_publish_button_use_the_active_language(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        language: str,
        document_names: tuple[str, str],
        publication_names: tuple[str, str],
        publish_label: str,
    ) -> None:
        revision = Revision.create_from_content(document, "# Published\n", editor)
        publication.publish(revision=revision, expected_revision_id=None)

        with override(language):
            assert (
                str(Document._meta.verbose_name),
                str(Document._meta.verbose_name_plural),
            ) == document_names
            assert (
                str(Publication._meta.verbose_name),
                str(Publication._meta.verbose_name_plural),
            ) == publication_names
            index = editor_client.get(
                reverse("admin:app_list", kwargs={"app_label": "llms_manager"})
            )
            assert index.status_code == 200
            models = {
                model["object_name"]: str(model["name"])
                for app in index.context["app_list"]
                for model in app["models"]
            }
            assert models["Document"] == document_names[1].capitalize()
            assert models["Publication"] == publication_names[1].capitalize()
            text = strip_tags(index.content.decode())
            assert document_names[1].capitalize() in text
            assert publication_names[1].capitalize() in text
            card = editor_client.get(change_url)
            assert card.status_code == 200
            form = PublicationFormParser(button="_publish")
            form.feed(card.content.decode())
            assert form.button_found
            assert form.fields["_publish"] == publish_label

    @pytest.mark.parametrize(
        ("language", "expected_status", "content_label", "preview_label"),
        [
            (
                "en",
                "This revision is currently published.",
                "Full text",
                "Preview revision",
            ),
            (
                "ru",
                "Эта версия сейчас опубликована.",
                "Полный текст",
                "Предпросмотр версии",
            ),
        ],
    )
    def test_current_publication_uses_the_active_language(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        language: str,
        expected_status: str,
        content_label: str,
        preview_label: str,
    ) -> None:
        revision = Revision.create_from_content(document, "# Published\n", editor)
        publication.publish(revision=revision, expected_revision_id=None)

        with override(language):
            response = editor_client.get(change_url)

        assert response.status_code == 200
        assert expected_status.encode("utf-8") in response.content
        assert f"<summary>{content_label}</summary>".encode() in response.content
        assert (
            f'name="show_diff" value="1">{preview_label}</button>'.encode()
            in response.content
        )
        assert b'class="llms-diff"' not in response.content
