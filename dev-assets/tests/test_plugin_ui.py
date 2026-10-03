"""Tests for the declarative plugin ui (metadata schema, settings store,
console endpoints, action invocation)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.api import console as console_api
from app.source_plugins.models import PluginMetadata
from app.services.plugin_settings import PluginSettingsStore


def _metadata(ui) -> PluginMetadata:
    return PluginMetadata.from_dict(
        {
            "contractVersion": "1.0",
            "id": "ui_source",
            "name": "测试源",
            "version": "0.1.0",
            "type": "source",
            "domains": ["example.com"],
            "baseUrls": ["https://example.com"],
            "capabilities": ["search", "detail", "toc", "chapter"],
            "auth": {"mode": "none"},
            "content": {"access": "free"},
            "tags": [],
            "ui": ui,
        }
    )


# ---- metadata ui schema -----------------------------------------------------


def test_ui_valid_schema_normalizes():
    metadata = _metadata(
        [
            {
                "title": "分组",
                "items": [
                    {"type": "text", "id": "key", "label": "密钥", "default": "abc"},
                    {"type": "button", "id": "go", "label": "执行", "action": "ui_go"},
                ],
            }
        ]
    )
    assert metadata.validate() == []
    assert metadata.ui_actions() == {"ui_go"}
    assert [item["id"] for item in metadata.ui_setting_items()] == ["key"]
    assert metadata.ui[0]["items"][0]["default"] == "abc"


def test_ui_allowed_for_official_sources():
    """官方/授权书源同样可以声明 ui（2026-10 起放开，无第三方限制）。"""
    metadata = _metadata(
        [
            {
                "title": "站点设置",
                "items": [
                    {"type": "toggle", "id": "verbose", "label": "详细日志", "default": False},
                    {"type": "button", "id": "status", "label": "查看状态", "action": "ui_status"},
                ],
            }
        ]
    )
    metadata.tags = ["official"]
    assert metadata.validate() == []
    assert metadata.is_official_source() is True
    assert metadata.ui_actions() == {"ui_status"}


def test_ui_flat_item_shorthand_becomes_group():
    metadata = _metadata([{"type": "toggle", "id": "v", "label": "开关", "default": False}])
    assert metadata.validate() == []
    assert len(metadata.ui) == 1
    assert metadata.ui[0]["items"][0]["type"] == "toggle"


@pytest.mark.parametrize(
    "ui,fragment",
    [
        ([{"items": [{"type": "magic", "id": "x", "label": "x"}]}], "invalid type"),
        ([{"items": [{"type": "text", "id": "", "label": "x"}]}], "id is required"),
        (
            [
                {"items": [{"type": "text", "id": "a", "label": "x"}, {"type": "text", "id": "a", "label": "y"}]},
            ],
            "duplicate id",
        ),
        ([{"items": [{"type": "text", "id": "a", "label": ""}]}], "label is required"),
        ([{"items": [{"type": "button", "id": "a", "label": "x", "action": "Bad-Name"}]}], "action"),
        ([{"items": [{"type": "select", "id": "a", "label": "x"}]}], "choices"),
        ([{"items": [{"type": "toggle", "id": "a", "label": "x", "default": "yes"}]}], "boolean"),
        ([{"items": [{"type": "number", "id": "a", "label": "x", "default": True}]}], "numeric"),
        ([{"items": [{"type": "color", "id": "a", "label": "x", "default": "red"}]}], "#RRGGBB"),
    ],
)
def test_ui_validation_errors(ui, fragment):
    metadata = _metadata(ui)
    assert any(fragment in error for error in metadata.validate())


# ---- settings store ---------------------------------------------------------


def test_settings_store_roundtrip(tmp_path):
    store = PluginSettingsStore(db_path=tmp_path / "s.db")
    assert store.get_values("p1") == {}
    store.set_values("p1", {"text": "v", "num": 3, "flag": True, "empty": None})
    assert store.get_values("p1") == {"text": "v", "num": 3, "flag": True, "empty": None}
    store.set_values("p1", {"num": 5})
    assert store.get_values("p1")["num"] == 5
    store.delete_value("p1", "text")
    assert "text" not in store.get_values("p1")
    store.delete_all("p1")
    assert store.get_values("p1") == {}


# ---- console endpoints ------------------------------------------------------


def _fake_plugin():
    recorded = []

    async def ui_status(ctx, payload):
        recorded.append(payload)
        return {"ok": True, "message": f"values={payload['values']}", "data": {}}

    metadata = SimpleNamespace(
        id="ui_src",
        name="UI 源",
        enabled=True,
        ui=[
            {
                "title": "G",
                "items": [
                    {"type": "button", "id": "s", "label": "状态", "action": "ui_status"},
                    {"type": "text", "id": "api_key", "label": "密钥", "default": "abc"},
                    {"type": "number", "id": "limit", "label": "条数", "default": 5},
                    {"type": "select", "id": "mode", "label": "模式", "choices": ["fast", "full"], "default": "fast"},
                ],
            }
        ],
        ui_actions=lambda: {"ui_status"},
        ui_setting_items=lambda: metadata.ui[0]["items"][1:],
    )
    source = SimpleNamespace(ui_status=ui_status)
    plugin = SimpleNamespace(metadata=metadata, source=source)
    return plugin, recorded


def _fake_scheduler(plugin):
    recorded = []

    async def fake_call(target_plugin, fn, timeout=None):
        return await fn()

    fake_ctx = SimpleNamespace(_fetcher=SimpleNamespace(close=None))
    async def close_fetcher():
        pass
    fake_ctx._fetcher.close = close_fetcher

    return SimpleNamespace(
        _plugins={"ui_src": plugin},
        _make_ctx=lambda plugin_id: fake_ctx,
        timeout_for_plugin=lambda plugin: 5,
        _call_plugin=fake_call,
    ), recorded


def test_get_plugin_ui_merges_saved_over_defaults(monkeypatch):
    store = PluginSettingsStore()
    store.set_values("ui_src", {"api_key": "xyz"})
    plugin, _recorder = _fake_plugin()
    scheduler, _ = _fake_scheduler(plugin)
    monkeypatch.setattr(console_api, "_plugin_scheduler", scheduler, raising=False)

    payload = console_api.get_plugin_ui("ui_src")
    assert payload["pluginId"] == "ui_src"
    assert payload["values"]["api_key"] == "xyz"
    assert payload["values"]["limit"] == 5
    assert payload["values"]["mode"] == "fast"
    assert payload["configured"] == ["api_key"]
    store.delete_all("ui_src")


def test_put_plugin_ui_validates_and_saves(monkeypatch):
    plugin, _recorder = _fake_plugin()
    scheduler, _ = _fake_scheduler(plugin)
    monkeypatch.setattr(console_api, "_plugin_scheduler", scheduler, raising=False)
    store = PluginSettingsStore()

    result = console_api.put_plugin_ui("ui_src", {"values": {"api_key": "k2", "limit": 20, "mode": "full"}})
    assert result["ok"] is True
    values = store.get_values("ui_src")
    assert values == {"api_key": "k2", "limit": 20, "mode": "full"}

    with pytest.raises(Exception) as exc:
        console_api.put_plugin_ui("ui_src", {"values": {"unknown": 1}})
    assert "不支持的字段" in str(exc.value.detail)
    with pytest.raises(Exception) as exc:
        console_api.put_plugin_ui("ui_src", {"values": {"mode": "bogus"}})
    assert "选项之一" in str(exc.value.detail)
    store.delete_all("ui_src")


def test_run_action_receives_effective_values(monkeypatch):
    import asyncio

    plugin, recorded = _fake_plugin()
    scheduler, _ = _fake_scheduler(plugin)
    monkeypatch.setattr(console_api, "_plugin_scheduler", scheduler, raising=False)
    store = PluginSettingsStore()
    store.set_values("ui_src", {"limit": 33})

    result = asyncio.run(console_api.run_plugin_ui_action("ui_src", "ui_status"))
    assert result["ok"] is True
    assert result["message"] == "values={'api_key': 'abc', 'limit': 33, 'mode': 'fast'}"
    assert recorded[0]["action"] == "ui_status"
    assert recorded[0]["values"]["limit"] == 33

    with pytest.raises(Exception) as exc:
        asyncio.run(console_api.run_plugin_ui_action("ui_src", "no_such_action"))
    assert "动作不存在" in str(exc.value.detail)
    store.delete_all("ui_src")
