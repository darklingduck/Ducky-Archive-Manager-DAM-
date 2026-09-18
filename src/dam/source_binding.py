"""Interface-independent verified Gmail source binding; no message or queue intake.

Only a validated Gmail adapter profile is accepted here. Credential/token data
never participates in the identity key. A changed profile address creates a new
source identity; reconciling account renames is a separate reviewed workflow.
"""

from pydantic import ValidationError

from dam.gmail import GmailAdapterError, GmailProfile, is_adapter_profile, normalize_profile_address
from dam.identifiers import new_object_id
from dam.items import SourceInstance
from dam.storage import Storage, StorageError


class SourceBindingError(ValueError):
    """Safe verified-source failure without mailbox address or token data."""


class SourceBindingService:
    def __init__(self, store: Storage):
        if type(store) is not Storage:
            raise SourceBindingError("A DAM Storage instance is required")
        self._store = store

    def bind_gmail_profile(self, profile: GmailProfile) -> SourceInstance:
        """Resolve one authenticated mailbox profile to a stable SRC identity."""
        if not is_adapter_profile(profile):
            raise SourceBindingError("A validated Gmail profile is required")
        try:
            validated = GmailProfile.model_validate(profile.model_dump(mode="python", exclude_unset=True))
            identity = normalize_profile_address(validated.email_address)
        except (GmailAdapterError, ValidationError, ValueError, TypeError, AttributeError):
            raise SourceBindingError("Invalid Gmail profile identity") from None
        existing = self._store.source_instance_by_identity("gmail", identity)
        if existing is not None:
            if existing.identity_status != "verified":
                raise SourceBindingError("Gmail source identity is not verified")
            return existing
        for _ in range(16):
            source = SourceInstance(source_instance_id=new_object_id("SRC"), provider="gmail",
                                    identity_status="verified", source_identity=identity)
            try:
                return self._store.register_source_instance(source, verified_profile=profile)
            except StorageError as error:
                if str(error) != "Source instance identity already has different provenance":
                    raise SourceBindingError("Cannot persist verified Gmail source") from None
        raise SourceBindingError("Cannot allocate verified Gmail source identity")
