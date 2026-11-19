from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

if TYPE_CHECKING:
    from pytest_django import Settings


@pytest.mark.django_db(transaction=True)
def test_document_label_migration_preserves_existing_data_and_revision_files(
    settings: Settings, tmp_path: Path
) -> None:
    """Check that document label changes preserve records and revision files."""
    settings.MEDIA_ROOT = tmp_path
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()

    try:
        old_apps = executor.migrate([("llms_manager", "0001_initial")]).apps
        OldDocument = old_apps.get_model("llms_manager", "Document")
        OldRevision = old_apps.get_model("llms_manager", "Revision")
        OldPublication = old_apps.get_model("llms_manager", "Publication")
        OldUser = old_apps.get_model(settings.AUTH_USER_MODEL)

        assert OldDocument._meta.verbose_name == "llms"
        assert OldDocument._meta.verbose_name_plural == "llms"
        author = OldUser.objects.create(username="legacy-editor")
        document = OldDocument.objects.create(name="Основной документ")
        other_document = OldDocument.objects.create(name="Полный документ")
        published = OldRevision.objects.create(
            document_id=document.pk,
            number=1,
            file=ContentFile("# Опубликовано\n\nТекст.  \n".encode(), name="root.txt"),
            created_by_id=author.pk,
        )
        draft = OldRevision.objects.create(
            document_id=document.pk,
            number=2,
            file=ContentFile(b"# Unpublished draft\n", name="draft.txt"),
            created_by_id=author.pk,
        )
        full = OldRevision.objects.create(
            document_id=other_document.pk,
            number=1,
            file=ContentFile(b"# Full content\n", name="full.txt"),
            created_by_id=None,
        )
        OldPublication.objects.create(
            directory="/", filename="llms.txt", revision_id=published.pk
        )
        OldPublication.objects.create(
            directory="/", filename="llms-full.txt", revision_id=full.pk
        )
        OldPublication.objects.create(
            directory="/courses/python/", filename="llms.txt", revision_id=full.pk
        )
        OldPublication.objects.create(
            directory="/legacy/", filename="llms.txt", revision_id=None
        )

        records = {
            model._meta.model_name: list(model.objects.order_by("pk").values())
            for model in (OldDocument, OldRevision, OldPublication)
        }
        files = {}
        for revision in (published, draft, full):
            with default_storage.open(revision.file.name, "rb") as stored_file:
                files[revision.file.name] = stored_file.read()

        new_apps = (
            MigrationExecutor(connection)
            .migrate([("llms_manager", "0002_change_verbose_name_document")])
            .apps
        )
        NewDocument = new_apps.get_model("llms_manager", "Document")

        assert NewDocument._meta.verbose_name == "document"
        assert NewDocument._meta.verbose_name_plural == "documents"
        for model_name, expected in records.items():
            model = new_apps.get_model("llms_manager", model_name)
            assert list(model.objects.order_by("pk").values()) == expected
        assert (
            new_apps.get_model(settings.AUTH_USER_MODEL)
            .objects.filter(pk=author.pk, username="legacy-editor")
            .exists()
        )
        for name, expected_content in files.items():
            with default_storage.open(name, "rb") as stored_file:
                assert stored_file.read() == expected_content
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
