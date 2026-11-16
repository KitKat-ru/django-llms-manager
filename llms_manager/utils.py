from uuid import uuid4


def revision_upload_to(instance, filename) -> str:
    return f"llms_manager/{uuid4().hex}.txt"
