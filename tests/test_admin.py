from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils.translation import override

from llms_manager.models import Document, Publication, Revision

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser
    from pytest_django import Settings


@pytest.mark.django_db
class TestDocumentAdmin:
    """Check document editing, immutable revision history, previews, and access."""

    @pytest.fixture
    def document(self) -> Document:
        return Document.objects.create(name="Main document")

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
    def editor_url(self, document: Document) -> str:
        return reverse(
            viewname="admin:llms_manager_document_editor",
            kwargs={"document_id": document.pk},
        )

    @pytest.fixture(autouse=True)
    def english_language(self) -> Iterator[None]:
        with override("en"):
            yield

    @pytest.fixture(autouse=True)
    def media_root(self, settings: Settings, tmp_path: Path) -> Path:
        settings.MEDIA_ROOT = tmp_path
        return tmp_path

    def test_upload_field_does_not_block_saving_typed_content(
        self, editor_client: Client, editor_url: str
    ) -> None:
        response = editor_client.get(editor_url)

        assert response.status_code == 200
        # The leading space matches the separator before the HTML required attribute.
        assert " required" not in str(response.context["upload_form"]["file"])

    def test_import_populates_editor_without_creating_revision(
        self, editor_client: Client, editor_url: str
    ) -> None:
        content = "# Проект\n\n> Описание  \n"
        uploaded = SimpleUploadedFile("llms.txt", content.encode("utf-8"))

        response = editor_client.post(
            editor_url,
            {"import_file": "1", "file": uploaded, "content": "Previous text"},
        )

        assert response.status_code == 200
        assert response.context["editor_form"]["content"].value() == content
        assert not Revision.objects.exists()
        assert Document.objects.count() == 1
        assert not Publication.objects.exists()

    def test_invalid_upload_preserves_editor_content(
        self, editor_client: Client, editor_url: str
    ) -> None:
        response = editor_client.post(
            editor_url,
            {
                "import_file": "1",
                "file": SimpleUploadedFile("llms.txt", b"\xff\xfe"),
                "content": "Unsaved text",
            },
        )

        assert response.status_code == 200
        assert "file" in response.context["upload_form"].errors
        assert response.context["editor_form"]["content"].value() == "Unsaved text"
        assert not Revision.objects.exists()

    def test_saving_revision_redirects_to_content_without_publishing(
        self,
        document: Document,
        editor_client: Client,
        editor_url: str,
        editor: AbstractUser,
    ) -> None:
        content = "# Project\n\nText with trailing spaces.  \n"

        response = editor_client.post(
            editor_url, {"save_revision": "1", "content": content}
        )

        revision = Revision.objects.get()
        review_url = reverse(
            viewname="admin:llms_manager_document_revision",
            kwargs={"document_id": document.pk, "revision_id": revision.pk},
        )
        assert response.status_code == 302
        assert response.url == review_url
        assert editor_client.get(review_url).status_code == 200
        assert revision.read_content() == content
        assert revision.created_by_id == editor.pk
        assert revision.document == document
        assert not Publication.objects.exists()

    def test_old_revision_can_be_loaded_into_editor(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        old = Revision.create_from_content(document, "# Old\n", editor)
        Revision.create_from_content(document, "# Current\n", editor)

        response = editor_client.get(
            reverse(
                viewname="admin:llms_manager_document_edit_revision",
                kwargs={"document_id": document.pk, "revision_id": old.pk},
            )
        )

        assert response.status_code == 200
        assert response.context["editor_form"]["content"].value() == "# Old\n"
        assert Revision.objects.count() == 2
        assert not Publication.objects.exists()

    def test_preview_returns_exact_text_without_publishing(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        content = "# Draft\n\n<script>alert(1)</script>\n"
        revision = Revision.create_from_content(document, content, editor)

        response = editor_client.get(
            reverse(
                viewname="admin:llms_manager_document_preview",
                kwargs={"document_id": document.pk, "revision_id": revision.pk},
            )
        )

        assert response.content == content.encode("utf-8")
        assert response["Content-Type"] == "text/plain; charset=utf-8"
        assert "no-store" in response["Cache-Control"]
        assert not Publication.objects.exists()

    def test_revision_content_is_escaped(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        revision = Revision.create_from_content(
            document, "# Draft\n<script>alert(1)</script>\n", editor
        )

        response = editor_client.get(
            reverse(
                viewname="admin:llms_manager_document_revision",
                kwargs={"document_id": document.pk, "revision_id": revision.pk},
            )
        )

        assert response.status_code == 200
        assert b"<script>alert(1)</script>" not in response.content
        assert b"&lt;script&gt;" in response.content
        assert b'class="llms-diff"' not in response.content

    def test_history_lists_only_own_revisions_and_all_publication_addresses(
        self, document: Document, editor: AbstractUser, editor_client: Client
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", editor)
        second = Revision.create_from_content(document, "# Second\n", editor)
        other = Document.objects.create(name="Other document")
        Revision.create_from_content(other, "# Other\n", editor)
        root = Publication.objects.create()
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        root.publish(revision=first, expected_revision_id=None)
        full.publish(revision=first, expected_revision_id=None)

        response = editor_client.get(
            reverse(
                viewname="admin:llms_manager_document_revisions",
                kwargs={"document_id": document.pk},
            )
        )

        assert response.status_code == 200
        assert [revision.pk for revision in response.context["page"]] == [
            second.pk,
            first.pk,
        ]
        assert b'href="/llms.txt"' in response.content
        assert b'href="/llms-full.txt"' in response.content

    def test_documents_can_be_created_and_renamed(self, editor_client: Client) -> None:
        response = editor_client.post(
            reverse(viewname="admin:llms_manager_document_add"),
            {"name": "Guide", "_continue": "1"},
        )

        document = Document.objects.get()
        change_url = reverse(
            viewname="admin:llms_manager_document_change",
            kwargs={"object_id": document.pk},
        )
        assert response.status_code == 302
        assert response.url == change_url
        assert editor_client.get(change_url).status_code == 200

        response = editor_client.post(
            change_url, {"name": "Renamed guide", "_save": "1"}
        )

        assert response.status_code == 302
        document.refresh_from_db()
        assert document.name == "Renamed guide"
        assert not Revision.objects.exists()
        assert not Publication.objects.exists()

    def test_document_list_links_to_each_editor(
        self, document: Document, editor_client: Client
    ) -> None:
        other = Document.objects.create(name="Other document")

        response = editor_client.get(
            reverse(viewname="admin:llms_manager_document_changelist")
        )

        assert response.status_code == 200
        assert {row.pk for row in response.context["cl"].queryset} == {
            document.pk,
            other.pk,
        }
        assert (
            reverse(
                viewname="admin:llms_manager_document_editor",
                kwargs={"document_id": other.pk},
            ).encode()
            in response.content
        )

    def test_editor_starts_from_own_latest_revision(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        editor_url: str,
    ) -> None:
        published = Revision.create_from_content(document, "# Published\n", editor)
        publication = Publication.objects.create()
        publication.publish(revision=published, expected_revision_id=None)
        Revision.create_from_content(document, "# Newer draft\n", editor)

        response = editor_client.get(editor_url)

        assert response.status_code == 200
        assert response.context["editor_form"]["content"].value() == "# Newer draft\n"

    def test_empty_editor_does_not_claim_other_addresses_are_empty(
        self, editor: AbstractUser, editor_client: Client, editor_url: str
    ) -> None:
        other = Document.objects.create(name="Other document")
        published = Revision.create_from_content(other, "# Published\n", editor)
        publication = Publication.objects.create()
        publication.publish(revision=published, expected_revision_id=None)

        response = editor_client.get(editor_url)

        assert response.status_code == 200
        assert response.context["editor_form"]["content"].value() == ""
        assert b"The public endpoint returns an empty file." not in response.content

    def test_staff_without_model_permission_cannot_preview(
        self,
        document: Document,
        editor: AbstractUser,
        client: Client,
        django_user_model: type[AbstractUser],
    ) -> None:
        staff = django_user_model.objects.create_user(username="staff", is_staff=True)
        revision = Revision.create_from_content(document, "# Draft\n", editor)
        client.force_login(staff)

        response = client.get(
            reverse(
                viewname="admin:llms_manager_document_preview",
                kwargs={"document_id": document.pk, "revision_id": revision.pk},
            )
        )

        assert response.status_code == 403

    def test_view_permission_allows_reading_but_not_saving(
        self,
        document: Document,
        editor: AbstractUser,
        editor_url: str,
        client: Client,
        django_user_model: type[AbstractUser],
    ) -> None:
        viewer = django_user_model.objects.create_user(username="viewer", is_staff=True)
        viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="llms_manager", codename="view_document"
            )
        )
        revision = Revision.create_from_content(document, "# Draft\n", editor)
        client.force_login(viewer)

        review = client.get(
            reverse(
                viewname="admin:llms_manager_document_revision",
                kwargs={"document_id": document.pk, "revision_id": revision.pk},
            )
        )
        saving = client.post(editor_url, {"content": "# New\n"})

        assert review.status_code == 200
        assert saving.status_code == 403
        assert Revision.objects.count() == 1

    def test_preview_requires_login(
        self, document: Document, editor: AbstractUser, client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Draft\n", editor)

        response = client.get(
            reverse(
                viewname="admin:llms_manager_document_preview",
                kwargs={"document_id": document.pk, "revision_id": revision.pk},
            )
        )

        assert response.status_code == 302
        assert response.url.startswith(reverse(viewname="admin:login"))

    @pytest.mark.parametrize("action", ["revision", "preview", "edit_revision"])
    def test_revision_must_belong_to_the_document_in_the_url(
        self,
        document: Document,
        editor: AbstractUser,
        editor_client: Client,
        action: str,
    ) -> None:
        other = Document.objects.create(name="Other document")
        revision = Revision.create_from_content(other, "# Other\n", editor)

        response = editor_client.get(
            reverse(
                viewname=f"admin:llms_manager_document_{action}",
                kwargs={"document_id": document.pk, "revision_id": revision.pk},
            )
        )

        assert response.status_code == 404
