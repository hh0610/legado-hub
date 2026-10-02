"""Tests for database initialization."""

import sqlite3
from pathlib import Path

import pytest

from app.storage.db import initialize_database


def test_initialize_database(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    result = initialize_database(db_path)
    assert Path(result).exists()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}

    expected = {
        "schema_meta",
        "books",
        "chapters",
        "update_tasks",
        "book_cache",
        "toc_cache",
        "chapter_cache",
        "book_records",
        "book_search_cache",
        "search_jobs",
        "search_results",
        "subscription_search_jobs",
        "aggregate_book_tasks",
        "user_book_subscriptions",
        "aggregate_chapter_tasks",
        "aggregate_book_sources",
        "aggregate_operation_logs",
        "aggregate_source_snapshots",
        "aggregate_source_snapshot_runs",
        "aggregate_ai_usage",
        "user_sessions",
        "users",
        "audit_events",
        "plugin_settings",
    }
    assert expected.issubset(tables)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(aggregate_chapter_tasks)")}
        assert "ai_self_score" in columns
        columns = {row[1] for row in conn.execute("PRAGMA table_info(aggregate_ai_usage)")}
        assert "ai_self_score" in columns
        columns = {row[1] for row in conn.execute("PRAGMA table_info(aggregate_source_snapshots)")}
        assert {"raw_content", "source_chapter_url", "word_count", "purify_audit_json"}.issubset(columns)
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        assert version == ("15",)

        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(user_book_subscriptions)"
        ).fetchall()
        assert {(row[2], row[3], row[4], row[6]) for row in foreign_keys} == {
            ("users", "user_id", "user_id", "CASCADE"),
            ("aggregate_book_tasks", "aggregate_book_id", "aggregate_book_id", "CASCADE"),
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(user_book_subscriptions)")
        }
        assert "idx_user_book_subscriptions_user_status" in indexes
        assert "idx_user_book_subscriptions_book" in indexes
        search_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(subscription_search_jobs)")
        }
        assert "idx_subscription_search_jobs_owner_updated" in search_indexes


def test_initialize_database_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    initialize_database(db_path)
    assert db_path.exists()


def test_schema_upgrade_rolls_back_when_migration_step_fails(tmp_path: Path, monkeypatch) -> None:
    import app.storage.db as db_module

    db_path = tmp_path / "failed-upgrade.db"

    def fail_migration(_conn):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(db_module, "_ensure_shared_library_schema", fail_migration)

    with pytest.raises(RuntimeError, match="migration failed"):
        db_module.initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "audit_events" not in tables
    assert "schema_meta" not in tables


def test_v13_session_invalidation_rolls_back_on_failure(tmp_path: Path, monkeypatch) -> None:
    import app.storage.db as db_module

    db_path = tmp_path / "failed-v13-upgrade.db"
    db_module.initialize_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, role) VALUES ('u1', 'reader', 'hash', 'user')"
        )
        conn.execute(
            "INSERT INTO user_sessions (session_id, user_id, expires_at) VALUES ('raw-session', 'u1', '2099-01-01')"
        )
        conn.execute("UPDATE schema_meta SET value = '12' WHERE key = 'version'")
        conn.commit()

    original_migration = db_module._migrate_v12_to_v13

    def fail_after_session_delete(conn):
        original_migration(conn)
        raise RuntimeError("v13 migration failed")

    monkeypatch.setattr(db_module, "_migrate_v12_to_v13", fail_after_session_delete)

    with pytest.raises(RuntimeError, match="v13 migration failed"):
        db_module.initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT user_id FROM user_sessions WHERE session_id = 'raw-session'"
        ).fetchone() == ("u1",)
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone() == ("12",)


def test_audit_service_persists_only_safe_summary_fields(tmp_path: Path) -> None:
    from app.services.audit import AuditService

    db_path = tmp_path / "audit.db"
    service = AuditService(db_path)
    service.record(
        action="subscription.update",
        actor_user_id="user-1",
        actor_role="user",
        target_type="subscription",
        target_id="user-1:book-1",
        summary={
            "status": "paused",
            "startChapterIndex": 12,
            "password": "never-store-this",
            "cookie": "ywkey=never-store-this",
            "content": "never-store-this",
        },
    )

    initialize_database(db_path)
    events = service.list_events()
    assert len(events) == 1
    assert events[0]["summary"] == {"status": "paused", "startChapterIndex": 12}
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT summary_json FROM audit_events").fetchone()[0]
    assert "never-store-this" not in stored


def test_v13_upgrade_preserves_users_and_invalidates_raw_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, role) VALUES ('u1', 'reader', 'hash', 'user')"
        )
        conn.execute(
            "INSERT INTO user_sessions (session_id, user_id, expires_at) VALUES ('s1', 'u1', '2099-01-01')"
        )
        conn.execute("DROP TABLE user_book_subscriptions")
        conn.execute("UPDATE schema_meta SET value = '8' WHERE key = 'version'")
        conn.commit()

    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT username FROM users WHERE user_id = 'u1'").fetchone() == ("reader",)
        assert conn.execute("SELECT user_id FROM user_sessions WHERE session_id = 's1'").fetchone() is None
        assert conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone() == ("15",)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_book_subscriptions'"
        ).fetchone() == (1,)


def test_v14_upgrade_backfills_raw_source_snapshot_content(tmp_path: Path) -> None:
    db_path = tmp_path / "v14.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO aggregate_source_snapshots (
                aggregate_book_id, chapter_index, source_id, clean_content, content_hash
            ) VALUES ('b1', 1, 'source_a', 'legacy cleaned body', 'hash')
            """
        )
        conn.execute("UPDATE schema_meta SET value = '13' WHERE key = 'version'")
        conn.commit()

    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT raw_content FROM aggregate_source_snapshots WHERE aggregate_book_id = 'b1'"
        ).fetchone() == ("legacy cleaned body",)
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone() == ("15",)


def test_v10_upgrade_adds_subscription_search_jobs_without_touching_users(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v10.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, role) VALUES ('u1', 'reader', 'hash', 'user')"
        )
        conn.execute("DROP TABLE subscription_search_jobs")
        conn.execute("UPDATE schema_meta SET value = '10' WHERE key = 'version'")
        conn.commit()

    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT username FROM users WHERE user_id = 'u1'"
        ).fetchone() == ("reader",)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscription_search_jobs'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone() == ("15",)


def test_v11_upgrade_backfills_shared_book_creator_from_create_log(tmp_path: Path) -> None:
    db_path = tmp_path / "v11.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO users (user_id, username, password_hash, role) VALUES (?, ?, 'hash', 'user')",
            [("u1", "creator"), ("u2", "existing")],
        )
        conn.executemany(
            "INSERT INTO aggregate_book_tasks (aggregate_book_id, name, added_by_user_id) VALUES (?, ?, ?)",
            [("b1", "Recoverable", ""), ("b2", "Keep Existing", "u2"), ("b3", "Unknown", "")],
        )
        conn.executemany(
            """
            INSERT INTO aggregate_operation_logs (
                aggregate_book_id, actor_user_id, actor_role, operation_type
            ) VALUES (?, ?, 'user', 'create')
            """,
            [("b1", "u1"), ("b2", "u1")],
        )
        conn.execute("UPDATE schema_meta SET value = '11' WHERE key = 'version'")
        conn.commit()

    initialize_database(db_path)

    with sqlite3.connect(db_path) as conn:
        creators = dict(conn.execute(
            "SELECT aggregate_book_id, added_by_user_id FROM aggregate_book_tasks"
        ).fetchall())
        assert creators == {"b1": "u1", "b2": "u2", "b3": ""}
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone() == ("15",)


def test_subscription_constraints_and_cascade(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, role) VALUES ('u1', 'reader', 'hash', 'user')"
        )
        conn.execute(
            "INSERT INTO aggregate_book_tasks (aggregate_book_id, name) VALUES ('b1', 'Book')"
        )
        conn.execute(
            "INSERT INTO user_book_subscriptions (user_id, aggregate_book_id) VALUES ('u1', 'b1')"
        )
        conn.execute(
            """
            INSERT INTO subscription_search_jobs (
                job_id, owner_user_id, keyword, page, status,
                payload_json, created_at, updated_at
            ) VALUES ('j1', 'u1', 'Book', 1, 'completed', '{}', 1, 1)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO user_book_subscriptions (user_id, aggregate_book_id, status) VALUES ('u1', 'b2', 'invalid')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO user_book_subscriptions (user_id, aggregate_book_id, start_chapter_index) VALUES ('u1', 'b1', 0)"
            )
        conn.execute("DELETE FROM aggregate_book_tasks WHERE aggregate_book_id = 'b1'")
        assert conn.execute(
            "SELECT 1 FROM user_book_subscriptions WHERE aggregate_book_id = 'b1'"
        ).fetchone() is None
        conn.execute("DELETE FROM users WHERE user_id = 'u1'")
        assert conn.execute(
            "SELECT 1 FROM subscription_search_jobs WHERE job_id = 'j1'"
        ).fetchone() is None
