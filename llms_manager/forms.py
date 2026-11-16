from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from llms_manager.models import Document, Revision


class EditorForm(forms.Form):
    """Accept complete Markdown text, including empty content and whitespace."""

    content = forms.CharField(
        label=_("Content"),
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 24, "class": "vLargeTextField"}),
        help_text=_("The complete document in Markdown."),
    )


class UploadForm(forms.Form):
    """Decode UTF-8 uploads into cleaned content, including empty files."""

    use_required_attribute = False

    file = forms.FileField(label=_("UTF-8 file"), allow_empty_file=True)

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        uploaded_file = cleaned_data.get("file")
        if uploaded_file is not None:
            try:
                cleaned_data["content"] = uploaded_file.read().decode("utf-8")
            except UnicodeDecodeError:
                self.add_error("file", _("Upload a UTF-8 text file."))
        return cleaned_data


class PublishForm(forms.Form):
    """Validate a selected revision and the expected publication baseline."""

    revision = forms.ModelChoiceField(
        queryset=Revision.objects.select_related("document"),
        widget=forms.HiddenInput,
    )

    expected_revision_id = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.HiddenInput,
    )


class DocumentSelectionForm(forms.Form):
    """Select a document for choosing one of its revisions."""

    document = forms.ModelChoiceField(
        queryset=Document.objects.order_by("name", "pk"),
        label=_("Document"),
    )


class RevisionSelectionForm(forms.Form):
    """Select a saved revision belonging to a particular document."""

    revision = forms.ModelChoiceField(
        queryset=Revision.objects.none(),
        label=_("Revision"),
    )

    def __init__(self, *args: Any, document: Document, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["revision"].queryset = document.revisions.all()
