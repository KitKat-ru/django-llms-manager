from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from re import findall, search
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.translation import override

from llms_manager.models import Document, Publication, Revision

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser
    from pytest_django import Settings


@pytest.mark.django_db
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

    @pytest.fixture
    def publish_url(self, publication: Publication) -> str:
        return reverse(
            viewname="admin:llms_manager_publication_publish",
            kwargs={"publication_id": publication.pk},
        )

    @pytest.fixture(autouse=True)
    def english_language(self) -> Iterator[None]:
        with override("en"):
            yield

    @pytest.fixture(autouse=True)
    def media_root(self, settings: Settings, tmp_path: Path) -> Path:
        settings.MEDIA_ROOT = tmp_path
        return tmp_path

    def _review_data(self, revision: Revision) -> dict[str, int | str]:
        return {
            "document": revision.document_id,
            "revision": revision.pk,
            "show_diff": "1",
        }

    def _publish_data(
        self, revision: Revision, expected_revision_id: int | None
    ) -> dict[str, int | str]:
        return {
            "revision": revision.pk,
            "expected_revision_id": (
                expected_revision_id if expected_revision_id is not None else ""
            ),
        }

    def test_address_can_be_created_with_a_filename_choice(
        self, editor_client: Client
    ) -> None:
        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {
                "directory": "courses/python",
                "filename": "llms-full.txt",
                "_continue": "1",
            },
        )

        publication = Publication.objects.get()
        assert response.status_code == 302
        assert response.url == reverse(
            viewname="admin:llms_manager_publication_change",
            kwargs={"object_id": publication.pk},
        )
        assert publication.directory == "/courses/python/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id is None
        assert editor_client.get(response.url).status_code == 200

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
        assert reviewing.context["publish_form"] is None
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

    def test_saving_a_new_address_preserves_selection_for_publication(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)
        selection = {
            "directory": "courses/python",
            "filename": "llms-full.txt",
            "document": document.pk,
            "revision": revision.pk,
        }
        review = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {**selection, "show_diff": "1"},
            follow=True,
        )

        response = editor_client.post(
            review.wsgi_request.get_full_path(),
            {**selection, "_continue": "1"},
            follow=True,
        )

        publication = Publication.objects.get()
        assert response.status_code == 200
        assert publication.directory == "/courses/python/"
        assert publication.filename == "llms-full.txt"
        assert publication.revision_id is None
        assert response.context["selected_document"].pk == document.pk
        assert response.context["candidate"].pk == revision.pk
        assert response.context["publish_form"] is not None
        assert editor_client.get(publication.get_absolute_url()).content == b""

        publishing = editor_client.post(
            reverse(
                viewname="admin:llms_manager_publication_publish",
                kwargs={"publication_id": publication.pk},
            ),
            self._publish_data(revision=revision, expected_revision_id=None),
        )

        assert publishing.status_code == 302
        publication.refresh_from_db()
        assert publication.revision_id == revision.pk
        assert (
            editor_client.get(publication.get_absolute_url()).content == b"# Selected\n"
        )

    def test_invalid_new_address_keeps_the_selected_revision(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Selected\n", editor)

        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {
                "directory": "/courses/",
                "filename": "other.txt",
                "document": document.pk,
                "revision": revision.pk,
                "_continue": "1",
            },
        )

        assert response.status_code == 200
        assert "filename" in response.context["adminform"].form.errors
        assert response.context["selected_document"].pk == document.pk
        assert response.context["candidate"].pk == revision.pk
        assert not Publication.objects.exists()

    def test_top_actions_require_review_before_publishing(
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
        save_button = search(r'<input\b[^>]*value="Save address"[^>]*>', html)
        publish_button = search(r'<input\b[^>]*value="Publish revision"[^>]*>', html)
        assert save_button is not None
        assert publish_button is not None
        assert 'form="publication_form"' in save_button.group()
        assert 'name="_continue"' in save_button.group()
        assert " disabled" not in save_button.group()
        assert " disabled" in publish_button.group()
        assert (
            save_button.start()
            < publish_button.start()
            < html.index('id="publication_form"')
        )
        assert 'id="llms-publication-publish-form"' not in html

    def test_other_filenames_are_rejected(self, editor_client: Client) -> None:
        response = editor_client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {"directory": "/", "filename": "other.txt", "_continue": "1"},
        )

        assert response.status_code == 200
        assert "filename" in response.context["adminform"].form.errors
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
        publish_url: str,
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
        publish_button = search(r'<input\b[^>]*value="Publish revision"[^>]*>', html)
        assert publish_button is not None
        assert " disabled" not in publish_button.group()
        assert 'form="llms-publication-publish-form"' in publish_button.group()
        assert publish_button.start() < html.index('id="publication_form"')
        assert publish_button.start() < html.index('class="llms-diff"')
        assert f'action="{publish_url}"' in html
        publication.refresh_from_db()
        assert publication.revision_id == current.pk

    def test_publication_selects_any_revision_and_records_an_admin_log(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        publish_url: str,
        change_url: str,
    ) -> None:
        other = Document.objects.create(name="Other document")
        Revision.create_from_content(other, "# Other\n", editor)
        selected = Revision.create_from_content(document, "# Selected\n", editor)
        Revision.create_from_content(document, "# Latest\n", editor)

        response = editor_client.post(
            publish_url,
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
        assert b"This revision is currently published." in review.content
        assert b'class="llms-diff"' not in review.content
        assert b"<details open>" in review.content
        assert f'href="{publication.get_absolute_url()}"'.encode() in review.content
        log = LogEntry.objects.get()
        assert log.content_type.model == "publication"
        assert log.object_id == str(publication.pk)
        assert document.name in log.change_message
        assert f"revision {selected.number}." in log.change_message

    def test_another_revision_with_identical_content_still_shows_diff(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Same\n", editor)
        candidate = Revision.create_from_content(document, "# Same\n", editor)
        publication.publish(revision=current, expected_revision_id=None)

        response = editor_client.get(change_url, self._review_data(revision=candidate))

        assert response.status_code == 200
        assert b'class="llms-diff"' in response.content
        assert b"This revision is currently published." not in response.content

    def test_final_newline_change_is_reported(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Content\n", editor)
        candidate = Revision.create_from_content(document, "# Content", editor)
        publication.publish(revision=current, expected_revision_id=None)

        response = editor_client.get(change_url, self._review_data(revision=candidate))

        assert response.status_code == 200
        assert b"Only line endings or the final newline differ." in response.content

    def test_stale_publication_returns_to_the_updated_diff(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        publish_url: str,
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        replacement = Revision.create_from_content(document, "# Replacement\n", editor)
        publication.publish(revision=first, expected_revision_id=None)
        publication.publish(revision=replacement, expected_revision_id=first.pk)

        response = editor_client.post(
            publish_url,
            self._publish_data(revision=candidate, expected_revision_id=first.pk),
            follow=True,
        )

        assert response.status_code == 200
        assert b"The published revision changed." in response.content
        assert response.context["candidate"].pk == candidate.pk
        assert response.context["published"].pk == replacement.pk
        publication.refresh_from_db()
        assert publication.revision_id == replacement.pk
        assert not LogEntry.objects.exists()

    def test_other_addresses_do_not_invalidate_the_review(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        publish_url: str,
    ) -> None:
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        first = Revision.create_from_content(document, "# First\n", editor)
        second = Revision.create_from_content(document, "# Second\n", editor)
        candidate = Revision.create_from_content(document, "# Candidate\n", editor)
        publication.publish(revision=first, expected_revision_id=None)
        full.publish(revision=second, expected_revision_id=None)
        full.publish(revision=candidate, expected_revision_id=second.pk)

        response = editor_client.post(
            publish_url,
            self._publish_data(revision=second, expected_revision_id=first.pk),
        )

        assert response.status_code == 302
        publication.refresh_from_db()
        full.refresh_from_db()
        assert publication.revision_id == second.pk
        assert full.revision_id == candidate.pk

    @pytest.mark.parametrize("already_published", [False, True])
    def test_save_address_preserves_selection_without_publishing(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        already_published: bool,
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
                "directory": publication.directory,
                "filename": publication.filename,
                "document": document.pk,
                "revision": revision.pk,
                "_continue": "1",
            },
            follow=True,
        )

        assert response.status_code == 200
        publication.refresh_from_db()
        expected_revision_id = current.pk if current else None
        assert publication.revision_id == expected_revision_id
        assert response.context["selected_document"].pk == document.pk
        assert response.context["candidate"].pk == revision.pk
        assert (
            response.context["publish_form"]["expected_revision_id"].value()
            == expected_revision_id
        )
        expected_content = b"# Published\n" if current else b""
        assert (
            editor_client.get(publication.get_absolute_url()).content
            == expected_content
        )

    @pytest.mark.parametrize("keep_selection", [False, True])
    def test_address_can_be_changed_without_changing_its_revision(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
        keep_selection: bool,
    ) -> None:
        revision = Revision.create_from_content(document, "# Content\n", editor)
        publication.publish(revision=revision, expected_revision_id=None)
        review = editor_client.get(
            change_url,
            {
                "directory": publication.directory,
                "filename": publication.filename,
                **self._review_data(revision=revision),
            },
        )

        response = editor_client.post(
            review.wsgi_request.get_full_path(),
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
        assert response.context["adminform"].form["directory"].value() == "/courses/"
        assert response.context["adminform"].form["filename"].value() == "llms-full.txt"
        assert not response.context["address_pending"]
        publication.refresh_from_db()
        assert publication.revision_id == revision.pk
        assert publication.get_absolute_url() == "/courses/llms-full.txt"
        assert editor_client.get("/llms.txt").status_code == 404
        assert (
            editor_client.get(publication.get_absolute_url()).content == b"# Content\n"
        )

    def test_metadata_save_preserves_a_concurrent_publication(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        change_url: str,
    ) -> None:
        current = Revision.create_from_content(document, "# Current\n", editor)
        replacement = Revision.create_from_content(document, "# Replacement\n", editor)
        publication.publish(revision=current, expected_revision_id=None)
        original_save = Publication.save

        def save_after_publication(
            instance: Publication, *args: Any, **kwargs: Any
        ) -> None:
            publication.publish(revision=replacement, expected_revision_id=current.pk)
            return original_save(instance, *args, **kwargs)

        with patch.object(
            Publication, "save", autospec=True, side_effect=save_after_publication
        ):
            response = editor_client.post(
                change_url,
                {
                    "directory": "/courses/",
                    "filename": publication.filename,
                    "document": document.pk,
                    "revision": current.pk,
                    "_continue": "1",
                },
                follow=True,
            )

        assert response.status_code == 200
        publication.refresh_from_db()
        assert publication.directory == "/courses/"
        assert publication.revision_id == replacement.pk
        assert response.context["candidate"].pk == current.pk
        assert response.context["published"].pk == replacement.pk
        assert (
            response.context["publish_form"]["expected_revision_id"].value()
            == replacement.pk
        )

    def test_view_permissions_allow_review_but_not_publication(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        client: Client,
        django_user_model: type[AbstractUser],
        change_url: str,
        publish_url: str,
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
            publish_url,
            self._publish_data(revision=revision, expected_revision_id=None),
        )

        assert review.status_code == 200
        assert review.context["candidate"].pk == revision.pk
        assert review.context["publish_form"] is None
        assert b'value="Save address"' not in review.content
        assert b'value="Publish revision"' not in review.content
        assert adding.status_code == 403
        assert publishing.status_code == 403
        publication.refresh_from_db()
        assert publication.revision_id is None

    def test_document_permission_is_required_to_select_private_revisions(
        self,
        document: Document,
        editor: AbstractUser,
        client: Client,
        django_user_model: type[AbstractUser],
        change_url: str,
        publish_url: str,
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
        client.force_login(publisher)

        card = client.get(change_url)
        review = client.get(change_url, self._review_data(revision=revision))
        new_review = client.post(
            reverse(viewname="admin:llms_manager_publication_add"),
            {"document": document.pk, "revision": revision.pk, "show_diff": "1"},
        )
        publishing = client.post(
            publish_url,
            self._publish_data(revision=revision, expected_revision_id=None),
        )

        assert card.status_code == 200
        assert card.context["document_form"] is None
        assert review.status_code == 403
        assert new_review.status_code == 403
        assert publishing.status_code == 403

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

    def test_publication_requires_post(
        self, editor_client: Client, publish_url: str
    ) -> None:
        assert editor_client.get(publish_url).status_code == 405

    def test_publication_requires_login(self, client: Client, publish_url: str) -> None:
        response = client.post(publish_url)

        assert response.status_code == 302
        assert response.url.startswith(reverse(viewname="admin:login"))

    def test_publication_requires_csrf(
        self, document: Document, editor: AbstractUser, publish_url: str
    ) -> None:
        revision = Revision.create_from_content(document, "# Draft\n", editor)
        client = Client(enforce_csrf_checks=True)
        client.force_login(editor)

        response = client.post(
            publish_url,
            self._publish_data(revision=revision, expected_revision_id=None),
        )

        assert response.status_code == 403

    def test_admin_log_failure_rolls_back_publication(
        self,
        document: Document,
        publication: Publication,
        editor: AbstractUser,
        editor_client: Client,
        publish_url: str,
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
                publish_url,
                self._publish_data(revision=candidate, expected_revision_id=current.pk),
            )

        publication.refresh_from_db()
        assert publication.revision_id == current.pk

    @pytest.mark.parametrize(
        ("language", "expected_status"),
        [
            ("en", "This revision is currently published."),
            ("ru", "Эта версия сейчас опубликована."),
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
    ) -> None:
        revision = Revision.create_from_content(document, "# Published\n", editor)
        publication.publish(revision=revision, expected_revision_id=None)

        with override(language):
            response = editor_client.get(change_url)

        assert response.status_code == 200
        assert expected_status.encode("utf-8") in response.content
        assert b'class="llms-diff"' not in response.content
