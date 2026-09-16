"""Contact resolver tests against real AddressBook fixtures."""

from __future__ import annotations

import sqlite3

import pytest

from macos_bridge.contacts import (
    _clean_label,
    _decode_photo,
    _detect_image_mime,
    _normalize_email,
    _normalize_phone,
    _strip_image_prefix,
    discover_source_dbs,
    empty_resolver,
)


def test_normalize_phone_strips_formatting():
    assert _normalize_phone("+1 (612) 555-0123") == "6125550123"
    assert _normalize_phone("(612) 555-0199") == "6125550199"
    assert _normalize_phone("6125550123") == "6125550123"
    assert _normalize_phone("+447700900123") == "7700900123"  # last 10 digits
    assert _normalize_phone("12345") == "12345"  # short numbers preserved
    assert _normalize_phone("") is None
    assert _normalize_phone(None) is None
    assert _normalize_phone("---") is None


def test_normalize_email_lowercases_and_strips():
    assert _normalize_email("Foo@Example.COM") == "foo@example.com"
    assert _normalize_email("  bar@example.com  ") == "bar@example.com"
    assert _normalize_email("") is None
    assert _normalize_email(None) is None


def test_clean_label_strips_apple_wrapper():
    assert _clean_label("_$!<Mobile>!$_") == "Mobile"
    assert _clean_label("_$!<Work>!$_") == "Work"
    assert _clean_label("_$!<Home>!$_") == "Home"
    assert _clean_label("_$!<WorkFAX>!$_") == "WorkFAX"
    assert _clean_label("phone") == "phone"  # already clean
    assert _clean_label("") is None
    assert _clean_label(None) is None


def test_strip_image_prefix_handles_apple_framing():
    # The 1-byte 0x01 prefix on AddressBook image blobs.
    png = b"\x01\x89PNG\r\n\x1a\nrest"
    assert _strip_image_prefix(png) == b"\x89PNG\r\n\x1a\nrest"
    # No prefix
    assert _strip_image_prefix(b"\x89PNG") == b"\x89PNG"
    assert _strip_image_prefix(None) is None
    assert _strip_image_prefix(b"") is None


def test_detect_image_mime():
    assert _detect_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert _detect_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert _detect_image_mime(b"GIF89a") == "image/gif"
    assert _detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPxx") == "image/webp"
    assert _detect_image_mime(b"\x00\x00\x00\x00") is None


def test_decode_photo_returns_bytes_and_mime():
    """The decoder strips Apple's 0x01 framing and returns raw bytes
    with the detected MIME — exactly what the MQTT image topic needs."""
    png_blob = b"\x01\x89PNG\r\n\x1a\nfake_image_data"
    result = _decode_photo(png_blob)
    assert result is not None
    raw, mime = result
    assert raw == b"\x89PNG\r\n\x1a\nfake_image_data"
    assert mime == "image/png"


def test_decode_photo_returns_none_for_unknown_format():
    assert _decode_photo(b"\x01garbage") is None
    assert _decode_photo(None) is None
    assert _decode_photo(b"") is None


def test_discover_source_dbs_finds_per_source_files(address_book_dir):
    dbs = discover_source_dbs(str(address_book_dir))
    assert len(dbs) >= 2
    assert all(p.endswith("AddressBook-v22.abcddb") for p in dbs)


def test_resolver_indexes_contacts(contact_resolver):
    # Use the private indices for sanity-checking — at least one entry each.
    assert len(contact_resolver._by_phone) > 0
    assert len(contact_resolver._by_email) > 0


# The AddressBook fixture is a snapshot of a real address book, so these tests
# pick a contact from it instead of naming a real person in the source.
def _a_contact_with_phone(resolver):
    for number, contact in resolver._by_phone.items():
        if len(number) == 10 and contact.first_name:
            return number, contact
    pytest.skip("no named contact with a 10-digit number in the fixture")


def _a_contact_with_email(resolver):
    for email, contact in resolver._by_email.items():
        if contact.first_name:
            return email, contact
    pytest.skip("no named contact with an email address in the fixture")


def test_resolve_phone_returns_full_contact_info(contact_resolver):
    """A number in the index resolves to its contact's name, label and UID."""
    number, expected = _a_contact_with_phone(contact_resolver)
    contact = contact_resolver.resolve_phone("+1" + number)
    assert contact is not None
    assert contact.uid == expected.uid
    assert contact.first_name
    assert contact.full_name.startswith(contact.first_name)
    assert contact.label is None or "_$!<" not in contact.label
    assert contact.uid is not None
    # UID should be the bare UUID (no :ABPerson suffix)
    assert ":" not in contact.uid


def test_resolve_phone_normalizes_input(contact_resolver):
    # Same number, three formats — all should resolve.
    number, _ = _a_contact_with_phone(contact_resolver)
    a = contact_resolver.resolve_phone("+1" + number)
    b = contact_resolver.resolve_phone(f"({number[:3]}) {number[3:6]}-{number[6:]}")
    c = contact_resolver.resolve_phone(number)
    assert a is not None and b is not None and c is not None
    assert a.uid == b.uid == c.uid


def test_resolve_email_returns_contact(contact_resolver):
    email, expected = _a_contact_with_email(contact_resolver)
    # Mixed case, as Apple stores many addresses.
    contact = contact_resolver.resolve_email(email.upper())
    assert contact is not None
    assert contact.uid == expected.uid
    assert contact.first_name == expected.first_name
    assert contact.label is None or "_$!<" not in contact.label


def test_resolve_handle_dispatches_by_at_sign(contact_resolver):
    email, by_email = _a_contact_with_email(contact_resolver)
    number, by_phone = _a_contact_with_phone(contact_resolver)
    a = contact_resolver.resolve_handle(email)
    b = contact_resolver.resolve_handle("+1" + number)
    assert a is not None and a.uid == by_email.uid
    assert b is not None and b.uid == by_phone.uid


def test_resolve_unknown_returns_none(contact_resolver):
    assert contact_resolver.resolve_phone("+19999999999") is None
    assert contact_resolver.resolve_email("nobody@example.com") is None
    assert contact_resolver.resolve_handle(None) is None


def test_payload_drops_none_fields(contact_resolver):
    """The to_payload() helper should omit fields that are None or empty."""
    number, _ = _a_contact_with_phone(contact_resolver)
    contact = contact_resolver.resolve_phone("+1" + number)
    assert contact is not None
    payload = contact.to_payload()
    for k, v in payload.items():
        assert v is not None
        assert v != ""


def test_photo_bytes_present_for_at_least_one_contact(contact_resolver):
    """At least one resolved contact should have raw photo bytes + a
    detected MIME type. Bytes are kept on the Contact dataclass for
    the bridge to publish to a per-entity HA MQTT image topic; they
    are deliberately *not* in to_payload()."""
    found = 0
    for c in contact_resolver._by_phone.values():
        if c.photo_bytes is not None:
            assert isinstance(c.photo_bytes, bytes)
            assert len(c.photo_bytes) > 0
            assert c.photo_mime in ("image/png", "image/jpeg", "image/gif", "image/webp")
            found += 1
            if found >= 3:
                break
    assert found > 0


def test_to_payload_excludes_binary_photo_fields(contact_resolver):
    """photo_bytes and photo_mime must never appear in the JSON payload —
    they're binary data that would break json.dumps and bloat the
    state-mirror topic."""
    for c in contact_resolver._by_phone.values():
        if c.photo_bytes is not None:
            payload = c.to_payload()
            assert "photo_bytes" not in payload
            assert "photo_mime" not in payload
            # And the legacy field name should be gone too.
            assert "photo_data_uri" not in payload
            return
    pytest.fail("no contact with a photo found in the fixture set")


def test_resolver_can_be_disabled_via_include_photo(address_book_dir):
    """When include_photo=False, no contact should have photo bytes set."""
    from macos_bridge.contacts import ContactResolver

    dbs = discover_source_dbs(str(address_book_dir))
    r = ContactResolver(dbs, include_photo=False, photo_field="thumbnail")
    for c in r._by_phone.values():
        assert c.photo_bytes is None
        assert c.photo_mime is None


def test_empty_resolver_returns_none_for_everything():
    r = empty_resolver()
    assert r.resolve_phone("+1 (612) 555-0123") is None
    assert r.resolve_email("a@example.com") is None
    assert r.resolve_handle("anything") is None


def test_address_book_resolves_call_history_at_scale(calls_db, contact_resolver):
    """How well does the resolver cover the user's actual call history?
    This is a soft assertion — don't fail if low, but log the rate."""
    with sqlite3.connect(f"file:{calls_db}?mode=ro", uri=True) as conn:
        addrs = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ZADDRESS FROM ZCALLRECORD WHERE ZADDRESS IS NOT NULL"
            )
        ]
    hits = sum(1 for a in addrs if contact_resolver.resolve_phone(a) is not None)
    # We expect at least 10% of distinct call peers to be in contacts.
    assert hits >= max(1, len(addrs) // 10), (
        f"only {hits}/{len(addrs)} distinct call addresses resolved"
    )
