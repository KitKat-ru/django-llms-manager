from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models, router, transaction
from django.db.models import Max
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from llms_manager.exceptions import PublicationConflict
from llms_manager.utils import revision_upload_to

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser

logger = logging.getLogger(__name__)


class Document(models.Model):
    """A named text document with its own immutable revision history."""

    class Meta:
        """Set display names for the document model."""

        verbose_name = _("llms")
        verbose_name_plural = _("llms")

    name = models.CharField(max_length=255, verbose_name=_("name"))

    def __str__(self) -> str:
        return self.name


class Revision(models.Model):
    """An immutable UTF-8 snapshot numbered within its document."""

    class Meta:
        """Set revision ordering, display names, and unique document numbers."""

        ordering = ("-pk",)
        verbose_name = _("revision")
        verbose_name_plural = _("revisions")
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=("document", "number"),
                name="llms_revision_document_number",
            )
        ]

    document = models.ForeignKey(
        Document,
        on_delete=models.PROTECT,
        related_name="revisions",
        verbose_name=_("document"),
    )
    number = models.PositiveIntegerField(editable=False, verbose_name=_("number"))
    file = models.FileField(
        upload_to=revision_upload_to,
        max_length=255,
        verbose_name=_("file"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )

    def __str__(self) -> str:
        return _("Revision %(number)s") % {"number": self.number}

    @classmethod
    def create_from_content(
        cls, document: Document, content: str, author: AbstractBaseUser | None
    ) -> Revision:
        revision = cls(document=document, created_by=author)
        revision.file.save(
            "revision.txt", ContentFile(content.encode("utf-8")), save=False
        )
        try:
            with transaction.atomic():
                revision.save()
        except Exception:
            try:
                revision.file.delete(save=False)
            except Exception:
                logger.exception(
                    "Failed to delete revision file %s after a database error.",
                    revision.file.name,
                )
            raise
        return revision

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError(
                _("Saved revisions cannot be edited. Create a new revision.")
            )
        kwargs["force_insert"] = True
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        kwargs["using"] = using
        with transaction.atomic(using=using):
            Document.objects.using(using).select_for_update().get(pk=self.document_id)
            revisions = (
                type(self).objects.using(using).filter(document_id=self.document_id)
            )
            latest_number = revisions.aggregate(number=Max("number"))["number"]
            self.number = (latest_number or 0) + 1
            return super().save(*args, **kwargs)

    def read_content(self) -> str:
        with self.file.open("rb") as revision_file:
            return revision_file.read().decode("utf-8")


class Publication(models.Model):
    """A public text file address with its independently selected revision."""

    class Meta:
        """Set publication display names and enforce unique file addresses."""

        verbose_name = _("publication")
        verbose_name_plural = _("publications")
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=("directory", "filename"),
                name="llms_publication_address",
            )
        ]

    class Filename(models.TextChoices):
        """Supported public file names."""

        LLMS = "llms.txt", "llms.txt"
        FULL = "llms-full.txt", "llms-full.txt"

    directory = models.CharField(
        max_length=255,
        default="/",
        blank=True,
        verbose_name=_("directory"),
    )
    filename = models.CharField(
        max_length=13,
        choices=Filename.choices,
        default=Filename.LLMS,
        verbose_name=_("file name"),
    )
    revision = models.ForeignKey(
        Revision,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="publications",
        verbose_name=_("published revision"),
    )

    def __str__(self) -> str:
        return f"{self.directory}{self.filename}"

    def clean(self) -> None:
        super().clean()
        directory = self.directory.strip().strip("/")
        if directory and (
            any(part in ("", ".", "..") for part in directory.split("/"))
            or any(character in directory for character in "\\?#\r\n")
        ):
            raise ValidationError(
                {
                    "directory": _(
                        "Enter a directory path without dot segments or URL parameters."
                    )
                }
            )
        self.directory = f"/{directory}/" if directory else "/"
        if len(self.directory) > self._meta.get_field("directory").max_length:
            raise ValidationError({"directory": _("The directory path is too long.")})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        kwargs = {"filename": self.filename}
        directory = self.directory.strip("/")
        if directory:
            kwargs["directory"] = directory
        return reverse(viewname="llms_manager:publication", kwargs=kwargs)

    def publish(self, *, revision: Revision, expected_revision_id: int | None) -> None:
        revision.read_content()
        with transaction.atomic():
            updated = (
                type(self)
                .objects.filter(
                    pk=self.pk,
                    revision_id=expected_revision_id,
                )
                .update(revision=revision)
            )
            if not updated:
                raise PublicationConflict(
                    _(
                        "The published revision changed. "
                        "Review the updated diff and try again."
                    )
                )
        self.revision = revision
