"""Files uploaded through ``put_attachment``, for ``send_email`` to attach.

An upload is two objects under ``uploads/<id>/``: ``meta.json`` (name,
content type and size, written when the URL is handed out) and ``data``
(the bytes, PUT by the client through the presigned URL). The bucket's
lifecycle rules delete both after a week.
"""

import dataclasses

from . import jsonvalue
from .store import ObjectStore

PREFIX = "uploads/"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Upload:
    """What ``put_attachment`` was told about a file.

    Attributes:
        name: The file name to give the attachment.
        content_type: Its MIME type.
        size: Its size in bytes (signed into the upload URL).
    """

    name: str
    content_type: str
    size: int


def upload_key(upload_id: str, name: str) -> str:
    """Return the key of one of an upload's objects.

    Args:
        upload_id: The upload's ULID.
        name: ``meta.json`` or ``data``.

    Returns:
        The S3 key.
    """
    return f"{PREFIX}{upload_id}/{name}"


def reserve(store: ObjectStore, upload_id: str, upload: Upload) -> None:
    """Record an upload's metadata.

    Args:
        store: The mail bucket.
        upload_id: The upload's ULID.
        upload: Its metadata.
    """
    record = dict(name=upload.name, content_type=upload.content_type, size=upload.size)
    store.put_new(upload_key(upload_id, "meta.json"), jsonvalue.dumps(record))


def read_upload(store: ObjectStore, upload_id: str) -> Upload:
    """Read an upload's metadata; raises NotFound if there is none.

    Args:
        store: The mail bucket.
        upload_id: The upload's ULID.

    Returns:
        The metadata.
    """
    record = jsonvalue.loads_dict(store.get(upload_key(upload_id, "meta.json")).body)
    return Upload(
        name=jsonvalue.get_str(record, "name"),
        content_type=jsonvalue.get_str(record, "content_type"),
        size=int(jsonvalue.get_number(record, "size")),
    )
