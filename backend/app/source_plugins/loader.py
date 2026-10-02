"""Discover and validate Python source plugins from plugins/sources."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

from app.config import PLUGINS_DIR
from app.source_plugins.models import PluginMetadata, LoadedPlugin
from app.source_plugins.errors import PluginValidationError


class PluginLoader:
    def __init__(self, plugins_dir: Path | None = None):
        self.plugins_dir = plugins_dir or PLUGINS_DIR
        self._plugins: dict[str, LoadedPlugin] = {}

    def load_all(self) -> dict[str, LoadedPlugin]:
        self._plugins = {}
        if not self.plugins_dir.exists():
            return self._plugins

        # Recursively scan for plugin directories (supports official/ thirdparty/ etc.)
        for metadata_path in sorted(self.plugins_dir.rglob("metadata.yaml")):
            plugin_dir = metadata_path.parent
            source_path = plugin_dir / "source.py"
            # Skip template scaffolding
            if plugin_dir.name == "source_plugin" and "templates" in str(plugin_dir):
                continue
            try:
                plugin = self._load_one(plugin_dir.name, metadata_path, source_path)
            except PluginValidationError:
                raise
            except Exception as exc:
                raise PluginValidationError(
                    f"Failed to load plugin from {plugin_dir}: {exc}"
                ) from exc
            if plugin.metadata.id in self._plugins:
                raise PluginValidationError(
                    f"Duplicate plugin ID: {plugin.metadata.id}"
                )
            self._plugins[plugin.metadata.id] = plugin
        return self._plugins

    def _load_one(
        self,
        dir_name: str,
        metadata_path: Path,
        source_path: Path,
    ) -> LoadedPlugin:
        raw_meta = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(raw_meta, dict):
            raise PluginValidationError(f"metadata.yaml must be a mapping: {dir_name}")
        metadata = PluginMetadata.from_dict(raw_meta)
        errors = metadata.validate()
        if errors:
            raise PluginValidationError(
                f"Plugin {metadata.id} validation failed: {'; '.join(errors)}"
            )

        if not source_path.exists():
            raise PluginValidationError(
                f"Plugin {metadata.id} missing source.py"
            )

        module = self._import_module(dir_name, source_path)
        source_cls = getattr(module, "Source", None)
        if source_cls is None:
            raise PluginValidationError(
                f"Plugin {metadata.id} source.py must export a Source class"
            )

        source_instance = source_cls()
        for cap in metadata.capabilities:
            method_names = ["auth_status"] if cap == "auth" else [cap]
            if cap == "explore":
                method_names = ["explore_groups", "explore"]
            for method_name in method_names:
                if not hasattr(source_instance, method_name) or not callable(
                    getattr(source_instance, method_name, None)
                ):
                    raise PluginValidationError(
                        f"Plugin {metadata.id} declares capability '{cap}' but Source has no async method '{method_name}'"
                    )

        for action in sorted(metadata.ui_actions()):
            if not callable(getattr(source_instance, action, None)):
                raise PluginValidationError(
                    f"Plugin {metadata.id} ui references action '{action}' but Source has no callable method '{action}'"
                )

        return LoadedPlugin(
            metadata=metadata,
            module=module,
            source=source_instance,
            capabilities=metadata.capabilities,
        )

    def _import_module(self, dir_name: str, source_path: Path) -> Any:
        path_key = hashlib.sha256(str(source_path.resolve()).encode("utf-8")).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(
            f"legadohub_plugin_{dir_name}_{path_key}", source_path
        )
        if spec is None or spec.loader is None:
            raise PluginValidationError(f"Cannot import {source_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())

