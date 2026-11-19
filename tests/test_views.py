from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django import VERSION
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve

from llms_manager.models import Document, Publication, Revision

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser
    from pytest_django import Settings


@pytest.mark.django_db
class TestLlmsView:
    """Check public files, revision caching, independent publishing, and URL routing."""

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

    @pytest.fixture(autouse=True)
    def cache_backend(self, settings: Settings) -> Iterator[None]:
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "llms-manager-view-tests",
            }
        }
        cache.clear()
        yield
        cache.clear()

    @pytest.fixture(params=["missing", "invalid_utf8"])
    def damage_file(self, request: pytest.FixtureRequest) -> Callable[[Revision], None]:
        def damage(revision: Revision) -> None:
            if request.param == "missing":
                revision.file.storage.delete(revision.file.name)
            else:
                Path(revision.file.path).write_bytes(b"\xff\xfe")

        return damage

    @pytest.mark.parametrize("timeout", [0, 60])
    @pytest.mark.parametrize("method", ["get", "head"])
    def test_unreadable_published_file_returns_400_without_caching_the_error(
        self,
        document: Document,
        publication: Publication,
        client: Client,
        settings: Settings,
        damage_file: Callable[[Revision], None],
        timeout: int,
        method: str,
    ) -> None:
        settings.LLMS_MANAGER_CACHE_TIMEOUT = timeout
        revision = Revision.create_from_content(document, "# Published\n", None)
        publication.publish(revision=revision, expected_revision_id=None)
        damage_file(revision)
        url = publication.get_absolute_url()

        failed = getattr(client, method)(url)
        Path(revision.file.path).write_bytes(b"# Restored\n")
        restored = client.get(url)

        assert failed.status_code == 400
        assert failed["Content-Type"] == "text/plain; charset=utf-8"
        assert failed.content == (
            b"The revision file is corrupted or missing." if method == "get" else b""
        )
        assert restored.status_code == 200
        assert restored.content == b"# Restored\n"
        publication.refresh_from_db()
        assert publication.revision_id == revision.pk
        assert Publication.objects.count() == 1
        assert Revision.objects.count() == 1

    def test_cached_text_remains_available_until_expiry_after_file_damage(
        self,
        document: Document,
        publication: Publication,
        client: Client,
        settings: Settings,
        damage_file: Callable[[Revision], None],
    ) -> None:
        settings.LLMS_MANAGER_CACHE_TIMEOUT = 5
        revision = Revision.create_from_content(document, "# Cached\n", None)
        publication.publish(revision=revision, expected_revision_id=None)
        url = publication.get_absolute_url()
        storage = revision.file.storage

        with (
            patch(
                "django.core.cache.backends.locmem.time.time", return_value=1000
            ) as clock,
            patch.object(storage, "open", wraps=storage.open) as open_file,
        ):
            first = client.get(url)
            damage_file(revision)
            clock.return_value = 1004
            cached = client.get(url)
            head = client.head(url)
            assert open_file.call_count == 1

            clock.return_value = 1005
            expired = client.get(url)

        assert first.status_code == 200
        assert cached.status_code == 200
        assert cached.content == first.content == b"# Cached\n"
        assert head.status_code == 200
        assert head.content == b""
        assert expired.status_code == 400
        assert open_file.call_count == 2

    @pytest.mark.parametrize("timeout", [0, 60])
    def test_published_revision_without_a_file_name_returns_400(
        self,
        document: Document,
        publication: Publication,
        client: Client,
        settings: Settings,
        timeout: int,
    ) -> None:
        settings.LLMS_MANAGER_CACHE_TIMEOUT = timeout
        revision = Revision.create_from_content(document, "# Published\n", None)
        publication.publish(revision=revision, expected_revision_id=None)
        Revision.objects.filter(pk=revision.pk).update(file="")

        response = client.get(publication.get_absolute_url())

        assert response.status_code == 400
        assert response.content == b"The revision file is corrupted or missing."

    def test_unknown_publication_is_not_created(self, client: Client) -> None:
        response = client.get("/llms.txt")

        assert response.status_code == 404
        assert not Document.objects.exists()
        assert not Publication.objects.exists()

    def test_configured_publication_is_empty_before_first_revision(
        self, publication: Publication, client: Client
    ) -> None:
        response = client.get(publication.get_absolute_url())

        assert response.status_code == 200
        assert response.content == b""
        assert response["Content-Type"] == "text/plain; charset=utf-8"
        assert Publication.objects.count() == 1
        assert not Document.objects.exists()

    @pytest.mark.parametrize("content", ("", "# Проект\n\n> Описание.\n"))
    def test_published_text_is_cached_while_publication_is_queried(
        self, document: Document, publication: Publication, client: Client, content: str
    ) -> None:
        revision = Revision.create_from_content(document, content, None)
        publication.publish(revision=revision, expected_revision_id=None)
        url = publication.get_absolute_url()
        storage = revision.file.storage

        with patch.object(storage, "open", wraps=storage.open) as open_file:
            first = client.get(url)
            with CaptureQueriesContext(connection) as queries:
                second = client.get(url)
                query_count = len(queries)
            head = client.head(url)

        assert first.content == content.encode("utf-8")
        assert second.content == first.content
        assert head.status_code == 200
        assert head.content == b""
        assert query_count == 1
        assert open_file.call_count == 1

    @pytest.mark.parametrize(("timeout", "expires_after"), ((None, 60), (5, 5)))
    def test_cached_text_expires_after_configured_timeout(
        self,
        document: Document,
        publication: Publication,
        client: Client,
        settings: Settings,
        timeout: int | None,
        expires_after: int,
    ) -> None:
        # Leave the setting absent when checking the default lifetime.
        if timeout is not None:
            settings.LLMS_MANAGER_CACHE_TIMEOUT = timeout
        revision = Revision.create_from_content(document, "# Cached\n", None)
        publication.publish(revision=revision, expected_revision_id=None)
        url = publication.get_absolute_url()
        storage = revision.file.storage

        with (
            patch(
                "django.core.cache.backends.locmem.time.time", return_value=1000
            ) as clock,
            patch.object(storage, "open", wraps=storage.open) as open_file,
        ):
            first = client.get(url)
            clock.return_value = 1000 + expires_after - 1
            before_expiry = client.get(url)
            assert open_file.call_count == 1

            clock.return_value = 1000 + expires_after
            after_expiry = client.get(url)

        assert first.content == b"# Cached\n"
        assert before_expiry.content == first.content
        assert after_expiry.content == first.content
        assert open_file.call_count == 2

    @pytest.mark.parametrize(
        ("timeout", "emits_warning"),
        (
            (0, False),
            (None, False),
            ("", True),
            ("60", True),
            ("invalid", True),
            (False, True),
            (True, True),
            (-1, True),
            (0.0, True),
            (1.5, True),
        ),
    )
    def test_disabled_timeout_bypasses_existing_cached_text(
        self,
        document: Document,
        publication: Publication,
        client: Client,
        settings: Settings,
        timeout: object,
        emits_warning: bool,
    ) -> None:
        revision = Revision.create_from_content(document, "# Uncached\n", None)
        publication.publish(revision=revision, expected_revision_id=None)
        url = publication.get_absolute_url()
        client.get(url)
        settings.LLMS_MANAGER_CACHE_TIMEOUT = timeout
        storage = revision.file.storage

        with (
            warnings.catch_warnings(record=True) as captured,
            patch.object(storage, "open", wraps=storage.open) as open_file,
        ):
            warnings.simplefilter("default", RuntimeWarning)
            first = client.get(url)
            second = client.get(url)

        assert first.content == b"# Uncached\n"
        assert second.content == first.content
        assert open_file.call_count == 2
        runtime_warnings = [
            warning
            for warning in captured
            if issubclass(warning.category, RuntimeWarning)
        ]
        assert len(runtime_warnings) == int(emits_warning)
        if emits_warning:
            assert "LLMS_MANAGER_CACHE_TIMEOUT" in str(runtime_warnings[0].message)
            assert "caching is disabled" in str(runtime_warnings[0].message)

    def test_publishing_and_restoring_revisions_updates_cached_output_immediately(
        self, document: Document, publication: Publication, client: Client
    ) -> None:
        first = Revision.create_from_content(document, "# First\n", None)
        second = Revision.create_from_content(document, "# Second\n", None)
        url = publication.get_absolute_url()

        assert client.get(url).content == b""

        publication.publish(revision=first, expected_revision_id=None)
        assert client.get(url).content == b"# First\n"

        publication.publish(revision=second, expected_revision_id=first.pk)
        assert client.get(url).content == b"# Second\n"

        publication.publish(revision=first, expected_revision_id=second.pk)
        storage = first.file.storage
        with patch.object(storage, "open", wraps=storage.open) as open_file:
            restored = client.get(url)

        assert restored.content == b"# First\n"
        open_file.assert_not_called()

    def test_cached_text_does_not_keep_moved_or_deleted_addresses_available(
        self, document: Document, publication: Publication, client: Client
    ) -> None:
        revision = Revision.create_from_content(document, "# Moved\n", None)
        publication.publish(revision=revision, expected_revision_id=None)
        previous_url = publication.get_absolute_url()
        assert client.get(previous_url).content == b"# Moved\n"

        publication.directory = "/catalog/courses/"
        publication.filename = Publication.Filename.FULL
        publication.save()
        current_url = publication.get_absolute_url()

        assert client.get(previous_url).status_code == 404
        assert client.get(current_url).content == b"# Moved\n"

        publication.delete()

        assert client.get(current_url).status_code == 404

    def test_draft_is_not_public(
        self,
        document: Document,
        publication: Publication,
        author: AbstractUser,
        client: Client,
    ) -> None:
        Revision.create_from_content(document, "# Draft\n", author)

        response = client.get(publication.get_absolute_url())

        assert response.content == b""

    def test_only_published_content_is_returned(
        self,
        document: Document,
        publication: Publication,
        author: AbstractUser,
        client: Client,
    ) -> None:
        content = "# Проект\n\n> Опубликованное описание.\n"
        published = Revision.create_from_content(document, content, author)
        publication.publish(revision=published, expected_revision_id=None)
        Revision.create_from_content(document, "# Draft\n", author)

        response = client.get(publication.get_absolute_url())

        assert response.status_code == 200
        assert response.content == content.encode("utf-8")
        assert response["Content-Type"] == "text/plain; charset=utf-8"

    @pytest.mark.parametrize(
        ("directory", "filename", "url"),
        [
            ("/", "llms.txt", "/llms.txt"),
            ("/", "llms-full.txt", "/llms-full.txt"),
            ("/courses/", "llms.txt", "/courses/llms.txt"),
            (
                "/courses/python/",
                "llms-full.txt",
                "/courses/python/llms-full.txt",
            ),
        ],
    )
    def test_root_and_nested_file_addresses(
        self,
        document: Document,
        author: AbstractUser,
        client: Client,
        directory: str,
        filename: str,
        url: str,
    ) -> None:
        publication = Publication.objects.create(directory=directory, filename=filename)
        revision = Revision.create_from_content(document, "# Content\n", author)
        publication.publish(revision=revision, expected_revision_id=None)

        response = client.get(url)

        assert publication.get_absolute_url() == url
        assert response.status_code == 200
        assert response.content == b"# Content\n"

    def test_publications_select_documents_and_revisions_independently(
        self,
        document: Document,
        publication: Publication,
        author: AbstractUser,
        client: Client,
    ) -> None:
        other = Document.objects.create(name="Other document")
        full = Publication.objects.create(filename=Publication.Filename.FULL)
        root_revision = Revision.create_from_content(document, "# Root\n", author)
        selected = Revision.create_from_content(other, "# Selected\n", author)
        Revision.create_from_content(other, "# Latest draft\n", author)
        publication.publish(revision=root_revision, expected_revision_id=None)
        full.publish(revision=selected, expected_revision_id=None)

        assert client.get("/llms.txt").content == b"# Root\n"
        assert client.get("/llms-full.txt").content == b"# Selected\n"

        publication.publish(revision=selected, expected_revision_id=root_revision.pk)
        storage = selected.file.storage
        with patch.object(storage, "open", wraps=storage.open) as open_file:
            assert client.get("/llms.txt").content == b"# Selected\n"
            assert client.get("/llms-full.txt").content == b"# Selected\n"

        open_file.assert_not_called()

    @pytest.mark.parametrize(
        "url",
        ["/courses/python/", "/robots.txt", "/notes.md", "/llms-other.txt"],
    )
    def test_unrelated_paths_are_not_captured(self, url: str) -> None:
        with pytest.raises(Resolver404):
            resolve(url)

    def test_post_is_not_allowed(self, client: Client) -> None:
        response = client.post("/llms.txt")

        assert response.status_code == 405

    @pytest.mark.skipif(
        VERSION < (5, 1), reason="LoginRequiredMiddleware requires Django 5.1."
    )
    def test_endpoint_stays_public_with_login_required_middleware(
        self, publication: Publication, client: Client, settings: Settings
    ) -> None:
        settings.MIDDLEWARE = [
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.auth.middleware.LoginRequiredMiddleware",
        ]

        response = client.get(publication.get_absolute_url())

        assert response.status_code == 200
        assert response.content == b""
