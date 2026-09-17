"""Shared pytest fixtures for the merged macos-mqtt-bridge test suite.

The comms-source fixtures (messages_db, calls_db, voicemail_db,
address_book_dir, contact_resolver) are synthetic by default, built by the
helpers in tests/fixtures/ — always available, including in CI. When a
private real-data snapshot is staged in `tmp/` (gitignored) — or at the
directory named by env var MACOS_BRIDGE_FIXTURE_DIR — that snapshot is used
instead, as an optional extra layer against real-shaped data. Either way,
every name/number/address the synthetic builders invent is fictional
(NANP 555-01xx numbers, example.com addresses, Alex/Jordan as the only
"contacts") and matches tests/fixtures/build_address_book_fixture.py's
synthetic AddressBook, so contact-enrichment assertions hold under either
fixture source.

The knowledgeC.db / RMAdminStore-Cloud.sqlite phase-ticker fixtures
(knowledge_fixture, rmadmin_cloud_fixture) are synthetic-only; there is no
real-snapshot variant for those.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

# ha-mqtt-bridge-toolkit lives in _shared/ (beside the project in the mono
# repo, inside it in the published repo) — fall back to a sys.path entry so
# `pytest` works even when the toolkit isn't pip-installed into the active
# environment. install.sh is the production path that installs it properly.
for _root in Path(__file__).resolve().parents[1:3]:
    _TOOLKIT_DIR = _root / "_shared" / "ha-mqtt-bridge-toolkit"
    if _TOOLKIT_DIR.is_dir():
        sys.path.insert(0, str(_TOOLKIT_DIR))
        break

from tests.fixtures.build_address_book_fixture import build_address_book_fixture  # noqa: E402
from tests.fixtures.build_calls_fixture import build_calls_fixture  # noqa: E402
from tests.fixtures.build_knowledge_fixture import ANCHOR_TODAY, build_fixture  # noqa: E402
from tests.fixtures.build_messages_fixture import build_messages_fixture  # noqa: E402
from tests.fixtures.build_rmadmin_cloud_fixture import build_rmadmin_cloud_fixture  # noqa: E402
from tests.fixtures.build_voicemail_fixture import (  # noqa: E402
    build_voicemail_assets_fixture,
    build_voicemail_fixture,
)

# config.example.yaml references ${DARWIN_USER_DIR} for the RMAdminStore paths;
# tests that load_config() the example need this set to *something* even if
# they never touch the resolved path.
os.environ.setdefault("DARWIN_USER_DIR", "/private/var/folders/test/dummy/0/")


# ---- synthetic fixtures (phase tickers) ------------------------------------


@pytest.fixture
def knowledge_fixture(tmp_path: Path) -> Path:
    """Synthetic knowledgeC.db with deterministic test data."""
    db = tmp_path / "knowledgeC.db"
    build_fixture(db)
    return db


@pytest.fixture
def rmadmin_cloud_fixture(tmp_path: Path) -> Path:
    """Synthetic RMAdminStore-Cloud.sqlite for Phase D tests."""
    db = tmp_path / "RMAdminStore-Cloud.sqlite"
    build_rmadmin_cloud_fixture(db)
    return db


@pytest.fixture
def anchor_today() -> datetime:
    """The datetime the fixture treats as 'today'."""
    return ANCHOR_TODAY


# ---- real-fixture fixtures (comms event sources) ---------------------------


def _fixture_dir() -> Path:
    override = os.environ.get("MACOS_BRIDGE_FIXTURE_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    return here.parents[1] / "tmp"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return _fixture_dir()


@pytest.fixture(scope="session")
def messages_db_src(fixture_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real chat.db snapshot when one is staged, otherwise a synthetic one
    built by tests/fixtures/build_messages_fixture.py — so the Messages
    classification tests run everywhere, including CI."""
    p = fixture_dir / "Messages" / "chat.db"
    if p.exists():
        return p
    db = tmp_path_factory.mktemp("synthetic-messages") / "chat.db"
    build_messages_fixture(db)
    return db


@pytest.fixture(scope="session")
def calls_db_src(fixture_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real CallHistory.storedata snapshot when one is staged, otherwise a
    synthetic one built by tests/fixtures/build_calls_fixture.py."""
    p = fixture_dir / "CallHistoryDB" / "CallHistory.storedata"
    if p.exists():
        return p
    db = tmp_path_factory.mktemp("synthetic-calls") / "CallHistory.storedata"
    build_calls_fixture(db)
    return db


@pytest.fixture(scope="session")
def voicemail_db_src(fixture_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real FaceTimeMessageStore-local.sqlitedb snapshot when one is
    staged, otherwise a synthetic one built by
    tests/fixtures/build_voicemail_fixture.py."""
    p = (
        fixture_dir
        / "group.com.apple.FaceTime"
        / "com.apple.facetimemessagestored"
        / "Data Store"
        / "FaceTimeMessageStore-local.sqlitedb"
    )
    if p.exists():
        return p
    db = tmp_path_factory.mktemp("synthetic-voicemail") / "FaceTimeMessageStore-local.sqlitedb"
    build_voicemail_fixture(db)
    return db


@pytest.fixture(scope="session")
def voicemail_assets_src(
    fixture_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path | None:
    """The real Assets/ snapshot when a real voicemail_db_src is in use,
    otherwise a synthetic Assets/ tree matched to the synthetic DB's
    ZRECORDUUID values (never a synthetic tree matched against a *real*
    DB — the UUIDs wouldn't line up, and the audio-path tests would fail
    instead of skipping)."""
    real_db = (
        fixture_dir
        / "group.com.apple.FaceTime"
        / "com.apple.facetimemessagestored"
        / "Data Store"
        / "FaceTimeMessageStore-local.sqlitedb"
    )
    if real_db.exists():
        p = real_db.parent / "Assets"
        return p if p.exists() else None
    assets_dir = tmp_path_factory.mktemp("synthetic-voicemail-assets")
    build_voicemail_assets_fixture(assets_dir)
    return assets_dir


@pytest.fixture(scope="session")
def address_book_dir(fixture_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real AddressBook snapshot when there is one, otherwise a synthetic
    address book of invented contacts, so the resolver is tested everywhere."""
    p = fixture_dir / "AddressBook"
    if p.exists():
        return p
    return build_address_book_fixture(tmp_path_factory.mktemp("AddressBook"))


@pytest.fixture(scope="session")
def contact_resolver(address_book_dir: Path):
    """Session-scoped real ContactResolver built from the fixture AddressBook
    sources. Photo encoding enabled."""
    from macos_bridge.contacts import ContactResolver, discover_source_dbs

    dbs = discover_source_dbs(str(address_book_dir))
    return ContactResolver(dbs, include_photo=True, photo_field="thumbnail")


@pytest.fixture
def messages_db(tmp_path: Path, messages_db_src: Path) -> Path:
    """Copy chat.db to a tmp location so tests can mutate state safely."""
    dst = tmp_path / "chat.db"
    shutil.copy2(messages_db_src, dst)
    return dst


@pytest.fixture
def calls_db(tmp_path: Path, calls_db_src: Path) -> Path:
    dst = tmp_path / "CallHistory.storedata"
    shutil.copy2(calls_db_src, dst)
    return dst


@pytest.fixture
def voicemail_db(tmp_path: Path, voicemail_db_src: Path) -> Path:
    dst = tmp_path / "FaceTimeMessageStore-local.sqlitedb"
    shutil.copy2(voicemail_db_src, dst)
    return dst


@pytest.fixture(scope="session")
def voicemail_assets_dir(voicemail_assets_src: Path | None) -> Path | None:
    return voicemail_assets_src


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"
