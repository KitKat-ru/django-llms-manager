from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client

from llms_manager.exceptions import PublicationConflict
from llms_manager.models import Document, Publication, Revision

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser
    from pytest_django import Settings


@pytest.mark.django_db
class TestRevisions:
    """Check immutable snapshots, storage failures, and publication changes."""

    @pytest.fixture
    def document(self) -> Document:
        return Document.objects.create(name="Main document")

    @pytest.fixture
    def publication(self) -> Publication:
        return Publication.objects.create()

    @pytest.fixture
    def author(self, django_user_model: type[AbstractUser]) -> AbstractUser:
        return django_user_model.objects.create_user(username="author")

    @pytest.fixture(autouse=True)
    def media_root(self, settings: Settings, tmp_path: Path) -> Path:
        settings.MEDIA_ROOT = tmp_path
        return tmp_path

    def test_saved_revision_preserves_unicode_and_whitespace(
        self, document: Document, author: AbstractUser
    ) -> None:
        content = "# Проект\n\n> Описание  \n\n- [Раздел](/docs/)\n"

        revision = Revision.create_from_content(document, content, author)

        assert revision.read_content() == content
        assert revision.created_by == author
        assert revision.document == document
        assert not Publication.objects.exists()

    def test_revision_numbers_start_at_one_for_each_document(
        self, document: Document, author: AbstractUser
    ) -> None:
        other = Document.objects.create(name="Other document")

        first = Revision.create_from_content(document, "# First\n", author)
        other_first = Revision.create_from_content(other, "# Other first\n", author)
        second = Revision.create_from_content(document, "# Second\n", author)
        other_second = Revision.create_from_content(other, "# Other second\n", author)

        assert list(
            Revision.objects.order_by("pk").values_list("document_id", "number")
        ) == [
            (document.pk, 1),
            (other.pk, 1),
            (document.pk, 2),
            (other.pk, 2),
        ]
        assert [
            first.number,
            other_first.number,
            second.number,
            other_second.number,
        ] == [
            1,
            1,
            2,
            2,
        ]

    def test_revision_numbers_are_unique_within_a_document(
        self, document: Document, author: AbstractUser
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", author)
        second = Revision.create_from_content(document, "# Second\n", author)

        with pytest.raises(IntegrityError), transaction.atomic():
            Revision.objects.filter(pk=second.pk).update(number=first.number)

        second.refresh_from_db()
        assert second.number == 2

    def test_new_revision_does_not_overwrite_existing_file(
        self, document: Document, author: AbstractUser
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", author)
        second = Revision.create_from_content(document, "# Second\n", author)

        assert first.file.name != second.file.name
        assert first.read_content() == "# First\n"
        assert second.read_content() == "# Second\n"

    def test_saved_revision_cannot_be_edited(
        self, document: Document, author: AbstractUser
    ) -> None:
        revision = Revision.create_from_content(document, "# Project\n", author)
        revision.created_by = None
        revision.number = 99

        with pytest.raises(ValidationError):
            revision.save()

        revision.refresh_from_db()
        assert revision.created_by_id == author.pk
        assert revision.number == 1
        assert revision.read_content() == "# Project\n"

    def test_database_failure_removes_uploaded_file(
        self, document: Document, author: AbstractUser, media_root: Path
    ) -> None:
        with (
            patch(
                "llms_manager.models.Revision.save",
                side_effect=IntegrityError("Database write failed"),
            ),
            pytest.raises(IntegrityError),
        ):
            Revision.create_from_content(document, "# Project\n", author)

        assert Document.objects.filter(pk=document.pk).exists()
        assert not Revision.objects.exists()
        assert list(media_root.rglob("*.txt")) == []

    def test_storage_failure_keeps_published_revision(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        published = Revision.create_from_content(document, "# Published\n", author)
        publication.publish(revision=published, expected_revision_id=None)
        storage = Revision._meta.get_field("file").storage

        with (
            patch.object(storage, "save", side_effect=OSError("Upload failed")),
            pytest.raises(OSError),
        ):
            Revision.create_from_content(document, "# New\n", author)

        publication.refresh_from_db()
        assert Revision.objects.count() == 1
        assert publication.revision_id == published.pk
        assert published.read_content() == "# Published\n"

    def test_stale_publication_does_not_replace_current_revision(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", author)
        second = Revision.create_from_content(document, "# Second\n", author)
        publication.publish(revision=first, expected_revision_id=None)

        with pytest.raises(PublicationConflict):
            publication.publish(revision=second, expected_revision_id=None)

        publication.refresh_from_db()
        assert publication.revision_id == first.pk

    def test_old_revision_can_be_published_again(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", author)
        second = Revision.create_from_content(document, "# Second\n", author)
        first_file = first.file.name
        publication.publish(revision=first, expected_revision_id=None)
        publication.publish(revision=second, expected_revision_id=first.pk)

        publication.publish(revision=first, expected_revision_id=second.pk)

        publication.refresh_from_db()
        assert publication.revision_id == first.pk
        assert Revision.objects.count() == 2
        assert first.file.name == first_file
        assert first.read_content() == "# First\n"

    def test_missing_candidate_file_keeps_published_revision(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        published = Revision.create_from_content(document, "# Published\n", author)
        candidate = Revision.create_from_content(document, "# Candidate\n", author)
        publication.publish(revision=published, expected_revision_id=None)
        candidate.file.storage.delete(candidate.file.name)

        with pytest.raises(FileNotFoundError):
            publication.publish(revision=candidate, expected_revision_id=published.pk)

        publication.refresh_from_db()
        assert publication.revision_id == published.pk

    def test_deleting_author_preserves_revision(
        self, document: Document, author: AbstractUser
    ) -> None:
        revision = Revision.create_from_content(document, "# Project\n", author)

        author.delete()

        revision.refresh_from_db()
        assert revision.created_by_id is None
        assert revision.read_content() == "# Project\n"

    def test_storage_without_local_paths_or_public_urls(
        self,
        document: Document,
        publication: Publication,
        author: AbstractUser,
        client: Client,
        settings: Settings,
    ) -> None:
        settings.STORAGES = {
            "default": {"BACKEND": "tests.storages.MemoryStorage"},
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }
        revision = Revision.create_from_content(document, "# Remote storage\n", author)

        publication.publish(revision=revision, expected_revision_id=None)
        response = client.get(publication.get_absolute_url())

        assert response.status_code == 200
        assert response.content == b"# Remote storage\n"
        with pytest.raises(NotImplementedError):
            _path = revision.file.path
        with pytest.raises(NotImplementedError):
            _url = revision.file.url

    def test_switching_documents_can_publish_an_older_revision(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        other = Document.objects.create(pk=document.pk + 1000, name="Other document")
        older = Revision.create_from_content(other, "# Older\n", author)
        newer = Revision.create_from_content(other, "# Newer\n", author)
        published = Revision.create_from_content(document, "# Published\n", author)
        publication.publish(revision=published, expected_revision_id=None)

        publication.publish(revision=older, expected_revision_id=published.pk)

        publication.refresh_from_db()
        assert publication.revision_id == older.pk
        assert publication.revision.document_id == other.pk
        assert list(other.revisions.values_list("pk", flat=True)) == [
            newer.pk,
            older.pk,
        ]
        assert list(document.revisions.values_list("pk", flat=True)) == [published.pk]

    def test_other_publications_do_not_invalidate_a_review(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        first = Revision.create_from_content(document, "# First\n", author)
        second = Revision.create_from_content(document, "# Second\n", author)
        candidate = Revision.create_from_content(document, "# Candidate\n", author)
        publication.publish(revision=first, expected_revision_id=None)
        full.publish(revision=second, expected_revision_id=None)

        full.publish(revision=candidate, expected_revision_id=second.pk)
        publication.publish(revision=second, expected_revision_id=first.pk)

        publication.refresh_from_db()
        full.refresh_from_db()
        assert publication.revision_id == second.pk
        assert full.revision_id == candidate.pk
        assert Publication.objects.count() == 2

    def test_stale_first_publication_keeps_the_configured_address_empty(
        self, document: Document, publication: Publication, author: AbstractUser
    ) -> None:
        revision = Revision.create_from_content(document, "# Candidate\n", author)

        with pytest.raises(PublicationConflict):
            publication.publish(revision=revision, expected_revision_id=revision.pk)

        publication.refresh_from_db()
        assert publication.revision_id is None
        assert Publication.objects.count() == 1

    def test_directory_and_filename_must_be_unique(
        self, publication: Publication
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Publication.objects.create(directory="///", filename=publication.filename)

        assert Publication.objects.count() == 1

    @pytest.mark.parametrize(
        ("directory", "expected"),
        [
            ("", "/"),
            ("/", "/"),
            ("courses", "/courses/"),
            ("/courses", "/courses/"),
            ("courses/python/", "/courses/python/"),
            ("/courses/python/", "/courses/python/"),
        ],
    )
    def test_directory_is_normalized(self, directory: str, expected: str) -> None:
        publication = Publication.objects.create(directory=directory)

        publication.refresh_from_db()
        assert publication.directory == expected

    @pytest.mark.parametrize(
        "directory",
        [
            "/../",
            "/courses/./",
            "/courses//python/",
            r"/courses\python/",
            "/courses?x=1/",
        ],
    )
    def test_invalid_directory_is_rejected(self, directory: str) -> None:
        with pytest.raises(ValidationError):
            Publication.objects.create(directory=directory)

        assert not Publication.objects.exists()

    def test_normalized_directory_length_is_validated(self) -> None:
        with pytest.raises(ValidationError):
            Publication.objects.create(directory="a" * 255)

        assert not Publication.objects.exists()
