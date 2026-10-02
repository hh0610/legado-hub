"""Per-plugin persisted settings (declared via metadata `ui:`).

Values are written by the admin console from the generic source-settings
dialog and read by the plugin runtime through ``ctx.settings``. Each key is
stored as its own JSON value so types (bool/number/string) survive round-trips.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.config import DB_PATH


class PluginSettingsStore:
    def __init__(self, db_path: str | Any = DB_PATH):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        from app.storage.db import initialize_database

        initialize_database(self.db_path)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_values(self, plugin_id: str) -> dict[str, Any]:
        if not plugin_id:
            return {}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value_json FROM plugin_settings WHERE plugin_id = ?",
                (plugin_id,),
            ).fetchall()
        values: dict[str, Any] = {}
        for key, raw in rows:
            try:
                values[str(key)] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
        return values

    def set_values(self, plugin_id: str, values: dict[str, Any]) -> None:
        if not plugin_id or not isinstance(values, dict) or not values:
            return
        payload = [
            (plugin_id, str(key), json.dumps(value, ensure_ascii=False))
            for key, value in values.items()
        ]
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO plugin_settings (plugin_id, key, value_json, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(plugin_id, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
            conn.commit()

    def delete_value(self, plugin_id: str, key: str) -> None:
        if not plugin_id or not key:
            return
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM plugin_settings WHERE plugin_id = ? AND key = ?",
                (plugin_id, str(key)),
            )
            conn.commit()

    def delete_all(self, plugin_id: str) -> None:
        if not plugin_id:
            return
        with self._conn() as conn:
            conn.execute("DELETE FROM plugin_settings WHERE plugin_id = ?", (plugin_id,))
            conn.commit()
