class PublicationConflict(Exception):
    """The published revision differs from the expected revision."""


class RevisionFileError(Exception):
    """The revision file is missing or cannot be decoded as UTF-8."""
