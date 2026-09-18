"""DAM-generated typed identities, independent of source-native identifiers."""

import base64
import re
from uuid import uuid4


def new_object_id(prefix: str, *, random_bytes: bytes | None = None) -> str:
    """Encode 128 random bits as 26 base32 characters after a type prefix."""
    if not isinstance(prefix, str) or re.fullmatch(r"[A-Z]{2,5}", prefix) is None:
        raise ValueError("invalid_identity_type")
    data = uuid4().bytes if random_bytes is None else random_bytes
    if not isinstance(data, bytes) or len(data) != 16:
        raise ValueError("invalid_random_identity")
    return prefix + "-" + base64.b32encode(data).decode("ascii").rstrip("=")
