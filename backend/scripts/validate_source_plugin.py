"""Validate a LegadoHub source plugin directory."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import yaml

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.source_plugins.models import PluginMetadata
from app.source_plugins.smoke import load_smoke_spec, _smoke_dir


FORBIDDEN_SOURCE_STRINGS = [
    "requests.",
    "httpx.",
    "threading",
    "asyncio.create_task",
    "asyncio.Semaphore",
    "asyncio.gather",
    "engine-jvm",
    "app.legado_engine",
    "app.engine",
    "demo_",
]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_metadata(path: Path) -> dict[str, Any]:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader) or {}
    if not isinstance(data, dict):
        raise ValueError("metadata.yaml must be a mapping")
    return data


def validate_plugin(plugin_dir: Path) -> list[str]:
    errors: list[str] = []
    metadata_path = plugin_dir / "metadata.yaml"
    source_path = plugin_dir / "source.py"
    readme_path = plugin_dir / "README.md"

    for path in (metadata_path, source_path, readme_path):
        if not path.exists():
            errors.append(f"missing required file: {path.name if path.parent == plugin_dir else path.relative_to(plugin_dir)}")
    if errors:
        return errors

    try:
        metadata_data = _load_metadata(metadata_path)
    except (ValueError, yaml.YAMLError) as exc:
        errors.append(str(exc))
        return errors
    metadata = PluginMetadata.from_dict(metadata_data)
    errors.extend(metadata.validate())
    if metadata.id != plugin_dir.name:
        errors.append(f"metadata id must match directory name: {plugin_dir.name}")
    if metadata.id.startswith("demo_"):
        errors.append("metadata id must not start with demo_")
    if not re.match(r"^[a-z0-9][a-z0-9_]*$", metadata.id):
        errors.append("metadata id must match ^[a-z0-9][a-z0-9_]*$")

    source_text = source_path.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_SOURCE_STRINGS:
        if forbidden in source_text:
            errors.append(f"source.py contains forbidden string: {forbidden}")

    source_obj = _load_source(source_path)
    source_id = getattr(source_obj, "id", "")
    if source_id != metadata.id:
        errors.append(f"Source.id must match metadata id: {metadata.id}")
    for cap in metadata.capabilities:
        method_name = cap if cap != "auth" else "auth_status"
        method = getattr(source_obj, method_name, None)
        if not callable(method):
            errors.append(f"Source missing method for capability: {cap}")
    for action in sorted(metadata.ui_actions()):
        if not callable(getattr(source_obj, action, None)):
            errors.append(f"ui references action '{action}' but Source has no callable method '{action}'")

    try:
        smoke_dir = _smoke_dir(plugin_dir)
        smoke_path = smoke_dir / "smoke.yaml"
        if smoke_path.exists():
            spec = load_smoke_spec(plugin_dir)
            fixtures = spec.get("fixtures") or {}
            required_stages = ["detail", "toc", "chapter"]
            if "search" in metadata.capabilities:
                required_stages.insert(0, "search")
            for stage in required_stages:
                fixture = fixtures.get(stage)
                if not isinstance(fixture, dict):
                    errors.append(f"smoke fixture missing stage: {stage}")
                    continue
                if not fixture.get("url") or not fixture.get("file"):
                    errors.append(f"smoke fixture {stage} must include url and file")
                fixture_file = smoke_dir / "fixtures" / str(fixture.get("file", ""))
                if not fixture_file.exists():
                    errors.append(f"smoke fixture file missing: {fixture_file.relative_to(plugin_dir)}")
            toc_expect = ((spec.get("expect") or {}).get("toc") or {})
            if not isinstance(toc_expect, dict):
                errors.append("expect.toc must be a mapping")
            elif toc_expect.get("complete"):
                expected_count = toc_expect.get("expectedCount")
                if not isinstance(expected_count, int) or expected_count <= 0:
                    errors.append("complete toc smoke requires a positive integer expect.toc.expectedCount")
                for field in ("expectedCount", "firstTitleContains", "lastTitleContains"):
                    if not toc_expect.get(field):
                        errors.append(f"complete toc smoke requires expect.toc.{field}")
                if toc_expect.get("requireUniqueChapterUrls") is not True:
                    errors.append("complete toc smoke requires expect.toc.requireUniqueChapterUrls: true")
                if toc_expect.get("requireSequentialIndexes") is not True:
                    errors.append("complete toc smoke requires expect.toc.requireSequentialIndexes: true")
            extra_fixtures = spec.get("extraFixtures") or []
            if not isinstance(extra_fixtures, list):
                errors.append("extraFixtures must be a list")
            else:
                for index, fixture in enumerate(extra_fixtures):
                    if not isinstance(fixture, dict) or not fixture.get("url") or not fixture.get("file"):
                        errors.append(f"extraFixtures[{index}] must include url and file")
                        continue
                    fixture_file = smoke_dir / "fixtures" / str(fixture["file"])
                    if not fixture_file.exists():
                        errors.append(f"smoke fixture file missing: {fixture_file.relative_to(plugin_dir)}")
    except Exception as exc:
        errors.append(f"invalid smoke.yaml: {exc}")

    return errors


def _load_source(source_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"validate_source_{source_path.parent.name}", source_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import source.py: {source_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_cls = getattr(module, "Source", None)
    if source_cls is None:
        raise ValueError("source.py must export Source")
    return source_cls()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a LegadoHub source plugin")
    parser.add_argument("--plugin", required=True)
    args = parser.parse_args()
    errors = validate_plugin(Path(args.plugin).resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
