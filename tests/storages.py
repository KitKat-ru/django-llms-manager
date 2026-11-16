from django.core.files.base import ContentFile, File
from django.core.files.storage import Storage


class MemoryStorage(Storage):
    """Keep file contents in memory without filesystem paths or public URLs."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def _open(self, name: str, mode="rb") -> ContentFile:
        return ContentFile(self.files[name], name=name)

    def _save(self, name: str, content: File) -> str:
        self.files[name] = b"".join(content.chunks())
        return name

    def exists(self, name: str) -> bool:
        return name in self.files

    def delete(self, name: str) -> None:
        self.files.pop(name, None)
