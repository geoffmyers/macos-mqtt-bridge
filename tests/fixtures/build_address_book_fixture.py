"""Build a synthetic AddressBook directory for the contact resolver tests.

Used when no real AddressBook snapshot is available (CI, a fresh clone). The
layout matches what `discover_source_dbs` looks for, and the schema is the
subset `ContactResolver` queries:

    ZABCDRECORD         Z_PK, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, ZMIDDLENAME,
                        ZNICKNAME, ZSUFFIX, ZORGANIZATION, ZJOBTITLE,
                        ZDEPARTMENT, ZTHUMBNAILIMAGEDATA, ZIMAGEDATA
    ZABCDPHONENUMBER    ZOWNER, ZFULLNUMBER, ZLABEL
    ZABCDEMAILADDRESS   ZOWNER, ZADDRESS, ZLABEL

Every name, number and address is invented: the numbers are in the NANP
fictional range (555-0100 to 555-0199) and the addresses use example.com.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Apple prefixes AddressBook image blobs with one 0x01 byte.
PNG_THUMBNAIL = b"\x01\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_THUMBNAIL = b"\x01\xff\xd8\xff\xe0" + b"\x00" * 16

SOURCES = {
    "11111111-1111-1111-1111-111111111111": [
        {
            "uid": "AAAAAAAA-0000-0000-0000-000000000001:ABPerson",
            "first": "Alex",
            "last": "Example",
            "thumbnail": PNG_THUMBNAIL,
            "phones": [("+1 (612) 555-0123", "_$!<Mobile>!$_")],
            "emails": [],
        },
    ],
    "22222222-2222-2222-2222-222222222222": [
        {
            "uid": "AAAAAAAA-0000-0000-0000-000000000002:ABPerson",
            "first": "Jordan",
            "last": "Example",
            "thumbnail": JPEG_THUMBNAIL,
            "phones": [("(612) 555-0199", "_$!<Home>!$_")],
            "emails": [("Jordan.Example@example.com", "_$!<Home>!$_")],
        },
    ],
}


def _build_db(path: Path, people: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE ZABCDRECORD (
                Z_PK INTEGER PRIMARY KEY, ZUNIQUEID TEXT,
                ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZMIDDLENAME TEXT, ZNICKNAME TEXT,
                ZSUFFIX TEXT, ZORGANIZATION TEXT, ZJOBTITLE TEXT, ZDEPARTMENT TEXT,
                ZTHUMBNAILIMAGEDATA BLOB, ZIMAGEDATA BLOB
            );
            CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT, ZLABEL TEXT);
            CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT, ZLABEL TEXT);
            """
        )
        for pk, person in enumerate(people, start=1):
            conn.execute(
                "INSERT INTO ZABCDRECORD (Z_PK, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME,"
                " ZTHUMBNAILIMAGEDATA) VALUES (?, ?, ?, ?, ?)",
                (pk, person["uid"], person["first"], person["last"], person["thumbnail"]),
            )
            conn.executemany(
                "INSERT INTO ZABCDPHONENUMBER VALUES (?, ?, ?)",
                [(pk, number, label) for number, label in person["phones"]],
            )
            conn.executemany(
                "INSERT INTO ZABCDEMAILADDRESS VALUES (?, ?, ?)",
                [(pk, address, label) for address, label in person["emails"]],
            )


def build_address_book_fixture(base: Path) -> Path:
    """Write <base>/Sources/<id>/AddressBook-v22.abcddb for each source and
    return <base>, the directory `discover_source_dbs` expects."""
    for source_id, people in SOURCES.items():
        _build_db(base / "Sources" / source_id / "AddressBook-v22.abcddb", people)
    return base
