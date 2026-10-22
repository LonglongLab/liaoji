"""Who the archive's owner is, as the archive itself can tell.

``source_accounts.display_name`` is what the source scan managed to write down,
and on a real machine that is a generated placeholder like ``微信账号 example``.
The name a person recognises has to be *derived*: the account's own outgoing
messages carry a sender identity, and the contact table often names it.

That derivation lives here, in one place, because two callers need the same
answer and must not be allowed to disagree about it:

- the local app's bootstrap, which puts the name on the top-bar chip;
- the outbox, which sends it to the remote archive so the remote's chip can
  say the same thing (PSU-2.40, record model v5).

The SQL is a constant rather than a function because the two callers hold
different kinds of connection (one raw ``sqlite3``, one SQLAlchemy). What must
not be duplicated is the *judgement* — which candidate counts as a name — and
that is :func:`resolve_account_display_name`.

The wxid-shape judgements (:func:`normalize_wxid`, the directory-name gate)
live here too since PSU-2.91: they are "whose archive is this" questions, and
the suffix definition must not drift between the identity walk and the
image-key derivation that first needed it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Candidates for "who am I", most-used identity first. Grouped rather than
# taken from one message because a single archive can hold several outgoing
# identities (a renamed account, an alias in one group), and the one that sent
# the most messages is the one the reader thinks of as themselves.
SELF_IDENTITY_CANDIDATES_SQL = """
SELECT
    m.sender_uid,
    m.sender_display_name,
    ct.display_name AS contact_display_name,
    ct.remark,
    ct.avatar_ref AS avatar_ref,
    COUNT(*) AS usage_count,
    MAX(m.created_at) AS last_message_at
FROM messages AS m
LEFT JOIN contacts AS ct
  ON ct.source_account_id = m.source_account_id
 AND ct.contact_uid = m.sender_uid
WHERE m.source_account_id = ? AND m.is_self = 1
GROUP BY
    m.sender_uid,
    m.sender_display_name,
    ct.display_name,
    ct.remark,
    ct.avatar_ref
ORDER BY usage_count DESC, last_message_at DESC
"""


def account_name_candidate(value: object, *, raw_ids: tuple[str, ...]) -> str | None:
    """One candidate, or ``None`` when it is really an identifier in disguise.

    A raw ``wxid_…`` or a copy of the account's own uid is not a name; showing
    it would be indistinguishable from showing the opaque id, while implying
    somebody chose it.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or candidate in raw_ids or candidate.casefold().startswith("wxid_"):
        return None
    return candidate


def resolve_account_display_name(
    candidates: Iterable[Mapping[str, object]],
    *,
    account_uid: str,
) -> str | None:
    """The account's own name, or ``None`` when nothing in the archive says.

    Walks the candidates in the order the SQL produced (most-used first) and
    takes the first row that yields any usable name, preferring the remark a
    person wrote over the contact's name over what the message carried.
    """
    for row in candidates:
        raw_ids = (account_uid, str(row["sender_uid"]))
        resolved = next(
            (
                candidate
                for candidate in (
                    account_name_candidate(row["remark"], raw_ids=raw_ids),
                    account_name_candidate(row["contact_display_name"], raw_ids=raw_ids),
                    account_name_candidate(row["sender_display_name"], raw_ids=raw_ids),
                )
                if candidate is not None
            ),
            None,
        )
        if resolved is not None:
            return resolved
    return None


# WeChat 4.x may suffix an account directory's name with `_xxxx` (4 hex
# digits) to disambiguate multiple logins of the same wxid on one machine.
_WXID_SUFFIX_RE = re.compile(r"^(.*)_([0-9a-fA-F]{4})$")

# The one directory-name shape whose suffix can be stripped unambiguously:
# a `wxid_` prefix, an alphanumeric body (no underscore — that is what makes
# the strip unambiguous), optionally one multi-login suffix. The suffix's
# hex alphabet is `_WXID_SUFFIX_RE`'s, verbatim.
_ACCOUNT_DIR_RE = re.compile(r"^wxid_([A-Za-z0-9]+)(?:_[0-9a-fA-F]{4})?$")


def normalize_wxid(account_dir_name: str) -> str:
    """Strip a trailing 4-hex-char `_xxxx` multi-login suffix from a wxid.

    That suffix is not part of the wxid used to derive the AES key, so it
    must be stripped first. A name with no suffix, or with a trailing
    `_xxxx` group that isn't all hex digits, is already the bare wxid and is
    returned unchanged.

    This lives with the identity judgements rather than the image-key module
    that first needed it, so that :func:`account_key_from_directory_name`
    shares one definition of the suffix instead of drifting from it.
    """
    match = _WXID_SUFFIX_RE.match(account_dir_name)
    if match is None:
        return account_dir_name
    return match.group(1)


def account_key_from_directory_name(account_uid: str) -> str | None:
    """The bare wxid a directory name spells, or ``None`` when it doesn't.

    ``source_accounts.account_uid`` is literally the data directory's name,
    and upstream discovery accepts almost anything as one (a ``wxid_`` prefix
    *or* any name longer than ten characters; ``--account-root`` is an
    arbitrary path). So this gate passes only the modern shape it can strip
    **unambiguously** — see ``_ACCOUNT_DIR_RE``. A prefixless custom id is
    refused even though it may well be the real account id: one ending in
    ``_`` + 4 hex would be stripped wrongly, the messages would later say
    otherwise, and the remote would re-key and reshuffle — worse than not
    falling back at all. Not sure means not said.

    Extraction uses this regex's own groups rather than chaining
    :func:`normalize_wxid`: a 4-hex body like ``wxid_7a7a`` parses here as a
    bare wxid with no suffix, while the chained strip would cut it to
    ``wxid``.
    """
    match = _ACCOUNT_DIR_RE.match(account_uid)
    if match is None:
        return None
    return f"wxid_{match.group(1)}"


def resolve_account_self_uid(
    candidates: Iterable[Mapping[str, object]],
    *,
    account_uid: str,
) -> str | None:
    """*Which WeChat account this is* — the messages first, the directory second.

    The bare wxid is the one thing on this account that is the same on every
    machine, and the account's own outgoing messages carry it verbatim.
    Everything else the archive knows it by is incidental to where it was
    captured: the source instance id is an HMAC under a seed minted at install
    time, the local ``acc_…`` is derived from both, and the data directory's
    name carries a per-install multi-login suffix.

    **The judgement here is the exact inverse of** :func:`account_name_candidate`,
    and the two must keep pointing at each other. That one exists to reject
    ``wxid_…``: showing a raw identifier as a name is indistinguishable from
    showing the opaque id, while implying somebody chose it. This one wants
    precisely the value that one rejects. Written as one function each rather
    than one function with a flag, because the flag is what would eventually
    let a caller ask for a name and get an identifier.

    No walking past a row, unlike the name: every ``sender_uid`` is a real
    identity, so "unusable candidate" has no meaning here. The first row is the
    most-used one, and the most-used identity is the one its owner thinks of as
    themselves — the same rule the name walk starts from.

    When the messages say nothing at all — a brand-new machine's first sync, a
    read-only archive, an import still running; however the derivation came up
    empty — the answer falls back to :func:`account_key_from_directory_name`
    (PSU-2.91). That is not a different value dressed up as this one: the
    directory name is the bare wxid plus the per-install suffix, so stripping
    the suffix yields exactly what the messages will say once they exist. The
    *raw* directory name still never travels; a name the gate cannot read
    unambiguously publishes ``None``, and the remote honestly keeps that
    stream in a group of its own.
    """
    for row in candidates:
        value = row.get("sender_uid")
        if not isinstance(value, str):
            continue
        uid = value.strip()
        if uid:
            return uid
    return account_key_from_directory_name(account_uid)


def resolve_account_avatar_ref(
    candidates: Iterable[Mapping[str, object]],
) -> str | None:
    """The account's own avatar, from the same candidates as the name.

    Deliberately *not* stopped at the row that produced the name: the name may
    come from the most-used identity while only another one carries a picture.
    ``None`` when the source held no avatar for this account — not invented.
    """
    row = _avatar_candidate(candidates)
    return None if row is None else str(row["avatar_ref"])


def resolve_account_avatar_uid(
    candidates: Iterable[Mapping[str, object]],
) -> str | None:
    """*Whose* picture the one above is, by the same walk (record model v10).

    The remote cannot be handed an ``avatar_ref``: that is a local resource
    path, and the bytes it names were never uploaded under it. What travels is
    the avatar ledger's blob id, and the ledger is keyed by contact uid — so
    the outbox needs the uid this walk settled on, not just the picture.

    Separate function rather than a wider return type because the local
    bootstrap has no use for the uid, and one of the two callers changing
    shape is how the two derivations would start to drift apart.
    """
    row = _avatar_candidate(candidates)
    return None if row is None else str(row["sender_uid"])


def _avatar_candidate(
    candidates: Iterable[Mapping[str, object]],
) -> Mapping[str, object] | None:
    """The first candidate that carries a picture at all."""
    for row in candidates:
        if row["avatar_ref"] is not None:
            return row
    return None
