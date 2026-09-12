"""Manual candidate selection (human override) tests.

Covers:
- scan_manual_candidates returns every candidate with gate diagnostics,
  including ones the automatic pipeline would reject;
- apply_manual_candidate writes a fallback chapter through the normal
  purify/persist pipeline, skips the automatic gates and records the audit
  marker;
- manually adopted chapters are excluded from the automatic processing queue;
- applying a chapter that is not among the matched entries is refused.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.services.aggregate_processor import AggregateProcessor
from app.services.library_books import LibraryBooksService
from app.services.shared_book_storage import SharedBookStorage
from app.source_plugins.id_codec import encode_chapter_id
from app.storage.db import initialize_database


OFFICIAL_SRC = "official_src"
SHORT_SRC = "short_src"
GOOD_SRC = "good_src"
BOOK_ID = "book:manual"
OFFICIAL_WORD_COUNT = 1000


def _setup_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "app_config.json"
    workflow = {
        "autoAggregate": True,
        "processAggregateOnRead": True,
        "aggregateCheckIntervalMinutes": 10,
        "purifyMode": "conservative",
        "aiEnabled": False,
        "useSharedBookStorage": True,
        "sharedBookStorageReadMode": "shared",
        "sharedBookStorageDualWrite": True,
        "minReadableChaptersForDiscovery": 2,
    }
    config_data: dict[str, Any] = {"aggregate": {"contentWorkflow": workflow}}
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    import app.core.app_config as _app_config_module

    _app_config_module.APP_CONFIG_PATH = config_path
    _app_config_module.AppConfig.reset()
    return db_path


def _insert_book(db_path: Path) -> None:
    payload = {
        "name": "测试书",
        "author": "作者",
        "primarySourceId": OFFICIAL_SRC,
        "primarySourceName": "官方源",
        "primaryBookId": f"{OFFICIAL_SRC}:{BOOK_ID}",
        "sources": [
            {"bookId": f"{OFFICIAL_SRC}:{BOOK_ID}", "sourceId": OFFICIAL_SRC,
             "sourceName": "官方源", "score": 100},
        ],
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO aggregate_book_tasks (
                aggregate_book_id, name, author, primary_book_id, primary_source_id,
                aggregate_payload_json, status, ai_enabled, start_chapter_index,
                initial_snapshot_last_index, auto_archive_on_complete, book_status,
                total_chapters_at_subscribe, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, 1, 0, 1, 'ongoing', 0,
                      datetime('now'), datetime('now'))
            """,
            (BOOK_ID, "测试书", "作者", f"{OFFICIAL_SRC}:{BOOK_ID}", OFFICIAL_SRC,
             json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()


def _seed_sources(db_path: Path, short_book_id: str, good_book_id: str) -> None:
    LibraryBooksService(db_path=db_path).save_payload_sources(BOOK_ID, [
        {"sourceId": OFFICIAL_SRC, "sourceName": "官方源",
         "bookId": f"{OFFICIAL_SRC}:{BOOK_ID}", "bookUrl": "https://official.example/book",
         "tocUrl": "", "score": 100},
        {"sourceId": SHORT_SRC, "sourceName": "偏短源", "bookId": short_book_id,
         "bookUrl": "https://short.example/book", "tocUrl": "", "score": 80},
        {"sourceId": GOOD_SRC, "sourceName": "合格源", "bookId": good_book_id,
         "bookUrl": "https://good.example/book", "tocUrl": "", "score": 80},
    ])
    storage = SharedBookStorage(root=db_path.parent / "library")
    source_refs = {
        "schemaVersion": 1,
        "bookId": BOOK_ID,
        "primarySource": {
            "sourceId": OFFICIAL_SRC,
            "sourceName": "官方源",
            "bookId": f"{OFFICIAL_SRC}:{BOOK_ID}",
            "bookUrl": "https://official.example/book",
            "tocUrl": "",
        },
        "sourceMapRefs": [
            {"sourceId": SHORT_SRC, "sourceName": "偏短源", "sourceBookId": short_book_id,
             "bookUrl": "https://short.example/book", "tocUrl": "",
             "lastVerifiedAt": datetime.now(timezone.utc).isoformat(),
             "status": "healthy", "priority": 80},
            {"sourceId": GOOD_SRC, "sourceName": "合格源", "sourceBookId": good_book_id,
             "bookUrl": "https://good.example/book", "tocUrl": "",
             "lastVerifiedAt": datetime.now(timezone.utc).isoformat(),
             "status": "healthy", "priority": 80},
        ],
    }
    storage.atomic_write_json(
        storage.source_refs_path(book_name="测试书", author="作者"),
        source_refs,
    )


def _insert_pending_chapter(db_path: Path, chapter_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO aggregate_chapter_tasks (
                chapter_id, aggregate_book_id, chapter_index, title, status,
                placeholder, source_chapter_id, source_word_count, preview_only,
                created_at, updated_at
            ) VALUES (?, ?, 2, '第2章', 'pending', 0, ?, ?, 0,
                      datetime('now'), datetime('now'))
            """,
            (chapter_id, BOOK_ID, f"{OFFICIAL_SRC}:official-ch2", OFFICIAL_WORD_COUNT),
        )
        conn.commit()


# 4 + 12*66 = 796 字 → length+80 < 1000 分类 preview，字数比 0.796 也不过门
SHORT_BODY = "候选正文" + ("这是一个很长的正文段落，" * 66)
# 4 + 12*83 = 1000 字 → 完整正文，字数门通过
GOOD_BODY = "候选正文" + ("这是一个很长的正文段落，" * 83)


class MultiSourceCatalog:
    """Serves distinct TOC/content maps per candidate source book id."""

    def __init__(self, maps: dict[str, dict[str, Any]]):
        self._maps = maps

    async def book_detail(self, book_id: str) -> dict[str, Any]:
        return {"data": {"name": "测试书", "author": "作者", "status": "ongoing",
                         "bookStatus": "ongoing", "coverUrl": "", "intro": "",
                         "wordCount": "10000"}}

    async def toc(self, book_id: str) -> dict[str, Any]:
        spec = self._maps.get(book_id)
        return {"chapters": [dict(ch) for ch in (spec["toc"] if spec else [])]}

    async def chapter(self, chapter_id: str) -> dict[str, Any]:
        for spec in self._maps.values():
            if chapter_id in spec["contents"]:
                content = spec["contents"][chapter_id]
                result: dict[str, Any] = {
                    "content": content, "title": "",
                    "sourceWordCount": OFFICIAL_WORD_COUNT,
                }
                if len(content) < 200:
                    result["extra"] = {"previewOnly": True}
                return result
        return {"content": "", "title": "", "sourceWordCount": OFFICIAL_WORD_COUNT,
                "extra": {"previewOnly": True}}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path)
    _insert_book(db_path)

    short_book_id = f"{SHORT_SRC}:https://short.example/book"
    good_book_id = f"{GOOD_SRC}:https://good.example/book"
    _seed_sources(db_path, short_book_id, good_book_id)

    short_ch_id = encode_chapter_id(SHORT_SRC, "https://short.example/ch2.html")
    good_ch_id = encode_chapter_id(GOOD_SRC, "https://good.example/ch2.html")
    chapter_row_id = f"{BOOK_ID}:ch2"
    _insert_pending_chapter(db_path, chapter_row_id)

    maps = {
        f"{OFFICIAL_SRC}:{BOOK_ID}": {
            "toc": [
                {"chapterId": encode_chapter_id(OFFICIAL_SRC, f"https://official.example/ch{i}.html"),
                 "title": f"第{i}章", "index": i}
                for i in range(1, 4)
            ],
            "contents": {},
        },
        short_book_id: {
            "toc": [{"chapterId": short_ch_id, "title": "第2章", "index": 2}],
            "contents": {short_ch_id: SHORT_BODY},
        },
        good_book_id: {
            "toc": [{"chapterId": good_ch_id, "title": "第2章", "index": 2}],
            "contents": {good_ch_id: GOOD_BODY},
        },
    }
    catalog = MultiSourceCatalog(maps)

    processor = AggregateProcessor(db_path)
    monkeypatch.setattr(processor, "_is_official_source", lambda sid: sid == OFFICIAL_SRC)

    async def _passthrough_ensure(_book_id, payload, **_kwargs):
        return payload

    monkeypatch.setattr(processor, "_ensure_candidate_sources_for_book", _passthrough_ensure)

    chapter_payload = {
        "chapterId": chapter_row_id,
        "sourceChapterId": f"{OFFICIAL_SRC}:official-ch2",
        "aggregateBookId": BOOK_ID,
        "title": "第2章",
        "chapterIndex": 2,
        "sourceWordCount": OFFICIAL_WORD_COUNT,
    }
    return {
        "db_path": db_path,
        "processor": processor,
        "catalog": catalog,
        "chapter": chapter_payload,
        "short_ch_id": short_ch_id,
        "good_ch_id": good_ch_id,
    }


def _chapter_row(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT status, preview_only, fallback_source_id, manual_supplement,
                   manual_supplement_json, source_alignment_json, content_file_path
            FROM aggregate_chapter_tasks WHERE chapter_id = ?
            """,
            (f"{BOOK_ID}:ch2",),
        ).fetchone()
    return dict(row)


@pytest.mark.asyncio
async def test_scan_returns_rejected_and_acceptable_candidates(environment):
    result = await environment["processor"].scan_manual_candidates(
        environment["catalog"], environment["chapter"],
    )

    assert result["officialWordCount"] == OFFICIAL_WORD_COUNT
    assert result["sourceErrors"] == []
    by_source = {item["sourceId"]: item for item in result["items"]}
    assert set(by_source) == {SHORT_SRC, GOOD_SRC}

    short = by_source[SHORT_SRC]
    assert short["classification"] == "preview"
    assert short["wordCount"]["passed"] is False
    assert short["autoAcceptable"] is False
    assert short["head"].startswith("候选正文")
    assert len(short["head"]) == 120
    assert short["tail"]
    assert short["sourceChapterId"] == environment["short_ch_id"]

    good = by_source[GOOD_SRC]
    assert good["classification"] == "full"
    assert good["wordCount"]["passed"] is True
    assert good["autoAcceptable"] is True


@pytest.mark.asyncio
async def test_apply_writes_manual_fallback_and_excludes_from_queue(environment):
    processor = environment["processor"]
    db_path = environment["db_path"]

    # Precondition: the pending chapter is in the automatic queue.
    queued_before = processor._chapters_for_processing(BOOK_ID, limit=50)
    assert any(row.get("chapterIndex") == 2 for row in queued_before)

    result = await processor.apply_manual_candidate(
        environment["catalog"],
        environment["chapter"],
        SHORT_SRC,
        environment["short_ch_id"],
    )

    assert result["ok"] is True
    assert result["manual"]["sourceId"] == SHORT_SRC
    assert result["manual"]["classification"] == "preview"
    assert result["manual"]["wordCount"]["passed"] is False

    row = _chapter_row(db_path)
    assert row["status"] == "fallback"
    assert row["preview_only"] == 0
    assert row["fallback_source_id"] == SHORT_SRC
    assert row["manual_supplement"] == 1
    assert row["content_file_path"]

    manual_meta = json.loads(row["manual_supplement_json"])
    assert manual_meta["sourceId"] == SHORT_SRC
    assert manual_meta["sourceChapterId"] == environment["short_ch_id"]
    alignment = json.loads(row["source_alignment_json"])
    assert alignment["crossSourceConsensusMode"] == "manual"
    assert alignment["manualSupplement"]["sourceId"] == SHORT_SRC

    # Manual chapters must never be picked by automatic update/retry queues.
    queued_after = processor._chapters_for_processing(BOOK_ID, limit=50)
    assert not any(row.get("chapterIndex") == 2 for row in queued_after)


@pytest.mark.asyncio
async def test_apply_rejects_unmatched_chapter_id(environment):
    bogus_id = encode_chapter_id(SHORT_SRC, "https://short.example/ch99.html")
    with pytest.raises(ValueError, match="manual_target_not_matched"):
        await environment["processor"].apply_manual_candidate(
            environment["catalog"],
            environment["chapter"],
            SHORT_SRC,
            bogus_id,
        )


@pytest.mark.asyncio
async def test_apply_rejects_unknown_source(environment):
    with pytest.raises(ValueError, match="manual_source_not_available"):
        await environment["processor"].apply_manual_candidate(
            environment["catalog"],
            environment["chapter"],
            "unknown_src",
            environment["short_ch_id"],
        )
