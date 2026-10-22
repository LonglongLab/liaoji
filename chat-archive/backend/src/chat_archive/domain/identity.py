"""Stable message identities and remote record keys."""

from __future__ import annotations

import hashlib


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def provider_message_key(
    *,
    provider: str,
    account_uid: str,
    source_message_id: str | None,
    source_instance_id: str,
    source_db_fingerprint: str,
    source_partition: str,
    source_table: str,
    source_row_id: int,
) -> str:
    """Return a deterministic provider identity, preferring a stable source ID."""
    if source_message_id:
        return f"{provider}:{account_uid}:{source_message_id}"

    coordinate = (
        f"{source_instance_id}|{source_db_fingerprint}|{source_partition}|"
        f"{source_table}|{source_row_id}"
    )
    return f"{provider}:{account_uid}:coord:{_stable_hash(coordinate)}"


def remote_record_key(
    *,
    provider: str,
    canonical_account_id: str,
    provider_message_key: str,
) -> str:
    """Return a deterministic, opaque identity for a remotely archived record."""
    logical_identity = f"{provider}||{canonical_account_id}||{provider_message_key}"
    return _stable_hash(logical_identity)


def remote_conversation_key(
    *,
    provider: str,
    account_key: str,
    conversation_uid: str,
) -> str:
    """A conversation's identity as *WeChat* knows it, not as this machine does.

    The local ``cv_…`` is minted from the machine-local account id
    (``_opaque_id("cv", account_id, conversation_uid)``), so the same chat
    captured on two machines gets two ids — and after two upload streams are
    recognised as one account, that is what puts two 文件传输助手 side by side
    under one name. This is the value that lets the remote see they are one.

    Salted by ``account_key`` rather than published bare, for the reason every
    other id in this module is salted: ``conversation_uid`` is a wxid or a
    chatroom id, and a bare hash of it would let two archives ask each other
    "do you also talk to this person". Within one account the salt is constant,
    so grouping still works.

    Deliberately **not** used to derive ``record_key``. Merging conversations
    moves rows between groups; it does not change any record's identity.
    """
    return _stable_hash(
        f"conversation||{provider}||{account_key}||{conversation_uid}"
    )


def remote_blob_id(
    *,
    provider: str,
    canonical_account_id: str,
    attachment_id: str,
) -> str:
    """Return a deterministic, opaque identity for a remotely stored attachment.

    Deliberately **not** derived from the attachment's bytes. A content hash
    would make the same file uploaded by two different people collide on one
    id, which turns "does this blob exist" into "does anyone else have this
    file" — a correlation oracle the archive has no reason to offer. Scoped by
    account for the same reason ``remote_record_key`` is: two archives on one
    remote must not be able to probe each other's contents by guessing.

    Derived from the local attachment id, which is itself opaque
    (``att_<sha256(message_id, kind)[:24]>``) and stable across re-imports, so
    a re-upload of the same attachment resolves to the same blob and the
    transfer's own idempotence takes over.

    64 lowercase hex characters satisfies ``^[A-Za-z0-9_-]{1,128}$`` and needs
    no URL escaping.

    PSU-2.82 added a content-hash *lookup* on the remote — ``batch-presign``
    can answer "those exact bytes are already here under another id" — and
    that does not weaken the argument above, because the lookup is locked
    inside one ``archive_id``. An archive belongs to one person; asking it
    whether it already holds a file tells its owner nothing they did not
    already know. What the argument rules out is asking *across* archives, and
    the ids stay salted per account precisely so that stays impossible. The
    claim exists because the salt makes the same picture land on two different
    ids when one WeChat account is captured on two machines — the correlation
    the salt prevents is between strangers, not between one person's own
    laptops.
    """
    return _stable_hash(f"blob||{provider}||{canonical_account_id}||{attachment_id}")


def avatar_blob_id(
    *,
    provider: str,
    canonical_account_id: str,
    content_sha256: str,
) -> str:
    """Return a deterministic, opaque identity for a remotely stored avatar.

    Derived from the *content* hash, unlike :func:`remote_blob_id` — but salted
    by the account for exactly the reason that one refuses a bare content hash.
    An avatar is the worst case for a bare content hash: the same person's
    picture is byte-identical in every archive that knows them, so "does this
    blob exist" would answer "do you also know this person". Salting by account
    keeps that shut.

    The salt deliberately does **not** include the contact, so one picture
    shared by several contacts of the same account — or the same contact
    referenced from several places — uploads once. Changing the picture changes
    the hash and therefore the id, so a re-upload of an unchanged avatar is a
    no-op and a changed one is a new object rather than an overwrite.

    The cost is that two accounts holding the same picture store it twice. On a
    real machine the secondary account has almost no contacts, so the duplicate
    volume is negligible, and paying it buys the same non-correlation the
    attachment ids have.
    """
    return _stable_hash(
        f"avatar||{provider}||{canonical_account_id}||{content_sha256}"
    )


def thumbnail_blob_id(attachment_blob_id: str) -> str:
    """Return the identity of the thumbnail belonging to an attachment's blob.

    Derived from the attachment's own blob id rather than recomputed from
    ``(provider, account, attachment_id)``, and that is the whole point: the
    reader holds nothing but the opaque id it found in ``attachment_refs``, so
    deriving from *that* is what lets a thumbnail be addressed without adding a
    single byte to the record wire format. Nothing has to be published for the
    far end to know this id — both ends can compute it from what they already
    have.

    Computed here rather than in the browser. Two reasons, and the second is the
    one that matters: ``crypto.subtle`` is asynchronous and needs a secure
    context, and more importantly a rule written once in Python and once in
    TypeScript is a rule that will eventually disagree with itself in a way
    neither end reports.

    The non-correlation property of :func:`remote_blob_id` survives, because its
    output is the input here: the provider and the account are still folded in,
    just one hash further down.
    """
    return _stable_hash(f"blob-thumb||{attachment_blob_id}")
