"""AddressBook contact resolver.

Reads all per-source AddressBook DBs under
~/Library/Application Support/AddressBook/Sources/<UUID>/AddressBook-v22.abcddb
and builds in-memory indices keyed by normalized phone (last 10 digits) and
lowercased email. Each lookup returns a friendly payload (first/last name,
organization, label, contact UID) plus the contact's photo as raw bytes
+ MIME type — kept *out* of ``to_payload()`` so the JSON event payload
stays small. The bridge publishes the bytes separately to a per-entity
HA MQTT image topic.

Photo blobs in ZTHUMBNAILIMAGEDATA / ZIMAGEDATA carry a 1-byte 0x01 framing
prefix; stripping it yields raw PNG or JPEG.

The resolver is built once at startup. To pick up newly-added contacts,
restart the bridge or call rebuild() (not currently scheduled).
"""

from __future__ import annotations

import glob
import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Apple labels are stored as `_$!<Label>!$_`; this strips the wrapper.
_LABEL_WRAP = re.compile(r"^_\$!<(.+?)>!\$_$")
_DIGITS = re.compile(r"\D+")


@dataclass(frozen=True)
class Contact:
    uid: str | None
    first_name: str | None
    last_name: str | None
    middle_name: str | None
    nickname: str | None
    suffix: str | None
    organization: str | None
    job_title: str | None
    department: str | None
    full_name: str
    label: str | None  # the matched phone/email label (Home/Work/Mobile/...)
    matched_value: str | None  # the original handle string from AddressBook
    # Raw image bytes + detected MIME ("image/png", "image/jpeg", ...) when
    # the contact has a photo in AddressBook. Deliberately excluded from
    # to_payload() — binary data has no place in a JSON state-mirror, and
    # the bytes get published to a dedicated HA MQTT image topic instead.
    photo_bytes: bytes | None = field(default=None, repr=False)
    photo_mime: str | None = None

    def to_payload(self) -> dict:
        d = asdict(self)
        # Strip the binary photo fields — they're published separately as
        # raw image bytes on the per-entity image topic.
        d.pop("photo_bytes", None)
        d.pop("photo_mime", None)
        return {k: v for k, v in d.items() if v is not None and v != ""}


def _normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = _DIGITS.sub("", raw)
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def _normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.strip().lower() or None


def _clean_label(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _LABEL_WRAP.match(raw)
    return m.group(1) if m else raw


def _build_full_name(first: str | None, last: str | None, org: str | None) -> str:
    parts = [p for p in (first, last) if p]
    if parts:
        return " ".join(parts)
    return org or ""


def _strip_image_prefix(blob: bytes | None) -> bytes | None:
    if not blob or len(blob) < 4:
        return None
    if blob[0] == 0x01:
        return blob[1:]
    return blob


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode_photo(blob: bytes | None) -> tuple[bytes, str] | None:
    """Strip Apple's 0x01 framing and detect MIME. Returns (bytes, mime)
    or None if the blob is empty / unrecognized. Replaces the old
    base64-data-URI encoder; the bridge now publishes raw bytes to a
    dedicated MQTT image topic instead of inlining a data URI in the
    JSON event payload."""
    raw = _strip_image_prefix(blob)
    if not raw:
        return None
    mime = _detect_image_mime(raw)
    if not mime:
        return None
    return raw, mime


@lru_cache(maxsize=4096)
def _normalize_phone_cached(raw: str) -> str | None:
    return _normalize_phone(raw)


class ContactResolver:
    """Builds an in-memory index from one or more AddressBook source DBs."""

    def __init__(
        self,
        source_dbs: list[str],
        *,
        include_photo: bool = True,
        photo_field: str = "thumbnail",  # "thumbnail" | "full" | "thumbnail_or_full"
    ):
        self.source_dbs = [Path(p) for p in source_dbs]
        self.include_photo = include_photo
        self.photo_field = photo_field
        self._by_phone: dict[str, Contact] = {}
        self._by_email: dict[str, Contact] = {}
        self.rebuild()

    def rebuild(self) -> None:
        by_phone: dict[str, Contact] = {}
        by_email: dict[str, Contact] = {}
        for db_path in self.source_dbs:
            if not db_path.exists():
                log.debug("contact source db missing: %s", db_path)
                continue
            try:
                self._load_one(db_path, by_phone, by_email)
            except Exception:  # noqa: BLE001
                log.exception("failed to index AddressBook source %s", db_path)
        self._by_phone = by_phone
        self._by_email = by_email
        log.info(
            "contact resolver indexed %d phone numbers and %d emails from %d source(s)",
            len(by_phone),
            len(by_email),
            sum(1 for p in self.source_dbs if p.exists()),
        )

    def _load_one(
        self,
        db_path: Path,
        by_phone: dict[str, Contact],
        by_email: dict[str, Contact],
    ) -> None:
        uri = f"file:{db_path.absolute()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row

            photo_col = self._photo_column()
            record_query = f"""
                SELECT
                    Z_PK,
                    ZUNIQUEID,
                    ZFIRSTNAME, ZLASTNAME, ZMIDDLENAME, ZNICKNAME, ZSUFFIX,
                    ZORGANIZATION, ZJOBTITLE, ZDEPARTMENT,
                    {photo_col} AS photo_blob
                FROM ZABCDRECORD
            """
            records: dict[int, dict] = {}
            for r in conn.execute(record_query):
                records[int(r["Z_PK"])] = dict(r)

            for pk, rec in records.items():
                decoded = (
                    _decode_photo(rec["photo_blob"])
                    if self.include_photo and rec["photo_blob"]
                    else None
                )
                photo_bytes = decoded[0] if decoded else None
                photo_mime = decoded[1] if decoded else None

                phones = list(
                    conn.execute(
                        "SELECT ZFULLNUMBER, ZLABEL FROM ZABCDPHONENUMBER WHERE ZOWNER = ?",
                        (pk,),
                    )
                )
                emails = list(
                    conn.execute(
                        "SELECT ZADDRESS, ZLABEL FROM ZABCDEMAILADDRESS WHERE ZOWNER = ?",
                        (pk,),
                    )
                )

                if not phones and not emails:
                    continue

                for ph in phones:
                    norm = _normalize_phone(ph["ZFULLNUMBER"])
                    if not norm:
                        continue
                    contact = self._make_contact(
                        rec,
                        label=_clean_label(ph["ZLABEL"]),
                        matched_value=ph["ZFULLNUMBER"],
                        photo_bytes=photo_bytes,
                        photo_mime=photo_mime,
                    )
                    by_phone.setdefault(norm, contact)

                for em in emails:
                    norm = _normalize_email(em["ZADDRESS"])
                    if not norm:
                        continue
                    contact = self._make_contact(
                        rec,
                        label=_clean_label(em["ZLABEL"]),
                        matched_value=em["ZADDRESS"],
                        photo_bytes=photo_bytes,
                        photo_mime=photo_mime,
                    )
                    by_email.setdefault(norm, contact)

    def _photo_column(self) -> str:
        if self.photo_field == "full":
            return "ZIMAGEDATA"
        if self.photo_field == "thumbnail":
            return "ZTHUMBNAILIMAGEDATA"
        # thumbnail_or_full
        return "COALESCE(ZTHUMBNAILIMAGEDATA, ZIMAGEDATA)"

    def _make_contact(
        self,
        rec: dict,
        *,
        label: str | None,
        matched_value: str | None,
        photo_bytes: bytes | None,
        photo_mime: str | None,
    ) -> Contact:
        uid_raw = rec.get("ZUNIQUEID") or ""
        uid = uid_raw.split(":", 1)[0] if uid_raw else None
        return Contact(
            uid=uid or None,
            first_name=rec.get("ZFIRSTNAME"),
            last_name=rec.get("ZLASTNAME"),
            middle_name=rec.get("ZMIDDLENAME"),
            nickname=rec.get("ZNICKNAME"),
            suffix=rec.get("ZSUFFIX"),
            organization=rec.get("ZORGANIZATION"),
            job_title=rec.get("ZJOBTITLE"),
            department=rec.get("ZDEPARTMENT"),
            full_name=_build_full_name(
                rec.get("ZFIRSTNAME"),
                rec.get("ZLASTNAME"),
                rec.get("ZORGANIZATION"),
            ),
            label=label,
            matched_value=matched_value,
            photo_bytes=photo_bytes,
            photo_mime=photo_mime,
        )

    def resolve_handle(self, handle: str | None) -> Contact | None:
        """Auto-detect phone vs email by '@' and dispatch."""
        if not handle:
            return None
        if "@" in handle:
            return self.resolve_email(handle)
        return self.resolve_phone(handle)

    def resolve_phone(self, number: str | None) -> Contact | None:
        norm = _normalize_phone(number)
        if not norm:
            return None
        return self._by_phone.get(norm)

    def resolve_email(self, address: str | None) -> Contact | None:
        norm = _normalize_email(address)
        if not norm:
            return None
        return self._by_email.get(norm)


def discover_source_dbs(address_book_dir: str) -> list[str]:
    """Find all AddressBook source DBs under <dir>/Sources/*/AddressBook-v22.abcddb,
    plus the top-level <dir>/AddressBook-v22.abcddb (which is usually empty but
    occasionally holds local-only entries)."""
    base = Path(address_book_dir)
    paths: list[str] = []
    top = base / "AddressBook-v22.abcddb"
    if top.exists():
        paths.append(str(top))
    for p in sorted(glob.glob(str(base / "Sources" / "*" / "AddressBook-v22.abcddb"))):
        paths.append(p)
    return paths


def empty_resolver() -> ContactResolver:
    """A no-op resolver useful when AddressBook is unavailable."""
    r = ContactResolver.__new__(ContactResolver)
    r.source_dbs = []
    r.include_photo = False
    r.photo_field = "thumbnail"
    r._by_phone = {}
    r._by_email = {}
    return r


def _all_contacts_iter(resolver: ContactResolver) -> Iterable[Contact]:
    """For diagnostics."""
    seen = set()
    for c in list(resolver._by_phone.values()) + list(resolver._by_email.values()):
        key = id(c)
        if key in seen:
            continue
        seen.add(key)
        yield c
