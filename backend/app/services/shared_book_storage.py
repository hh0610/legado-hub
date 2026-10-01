"""File-backed shared-book storage helpers."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.config import DATA_DIR

RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

TRACE_BEGIN = "LEGADOHUB_TRACE_BEGIN"
TRACE_END = "LEGADOHUB_TRACE_END"
CHAPTER_STATUS_PROCESSED = {"proofread_complete", "supplemented", "readable", "suspect", "fetched"}
CHAPTER_STATUS_READABLE = {"proofread_complete", "supplemented", "readable", "suspect"}
CHAPTER_STATUS_PREVIEW = {"fetched"}
CHAPTER_STATUS_FAILED = {"failed"}


def _safe_segment(value: str, *, max_length: int = 96) -> str:
    normalized = str(value or "").strip().strip(".")
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        normalized = "unknown"
    if normalized.upper() in RESERVED_WINDOWS_NAMES:
        normalized = f"{normalized}_"
    return normalized[:max_length].rstrip(" .") or "unknown"


def _book_folder_name(book_name: str, author: str) -> str:
    safe_name = _safe_segment(book_name or "unknown-book", max_length=80)
    safe_author = _safe_segment(author or "", max_length=40)
    if safe_author:
        return f"{safe_name}_{safe_author}"
    return safe_name


def _chapter_file_name(chapter_index: int, title: str) -> str:
    safe_title = _safe_segment(title or "untitled-chapter", max_length=120)
    return f"{chapter_index:04d}-{safe_title}.md"


class SharedBookStorage:
    """Resolve shared-book paths and perform atomic file writes."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else DATA_DIR / "library"
        self.private_root = self.root.parent / "library_private"

    def shared_book_dir(self, *, book_name: str, author: str) -> Path:
        return self.root / _book_folder_name(book_name, author)

    def metadata_path(self, *, book_name: str, author: str) -> Path:
        return self.shared_book_dir(book_name=book_name, author=author) / "metadata.json"

    def chapter_index_path(self, *, book_name: str, author: str) -> Path:
        return self.shared_book_dir(book_name=book_name, author=author) / "chapter_index.json"

    def chapters_dir(self, *, book_name: str, author: str) -> Path:
        return self.shared_book_dir(book_name=book_name, author=author) / "chapters"

    def source_snapshots_dir(self, *, book_name: str, author: str, source_id: str) -> Path:
        return (
            self.shared_book_dir(book_name=book_name, author=author)
            / "sources"
            / _safe_segment(source_id or "unknown-source", max_length=96)
            / "chapters"
        )

    def source_snapshot_path(
        self,
        *,
        book_name: str,
        author: str,
        source_id: str,
        chapter_index: int,
    ) -> Path:
        return self.source_snapshots_dir(
            book_name=book_name,
            author=author,
            source_id=source_id,
        ) / f"{chapter_index:06d}.json"

    def source_snapshot_manifest_path(self, *, book_name: str, author: str, source_id: str) -> Path:
        return self.source_snapshots_dir(
            book_name=book_name,
            author=author,
            source_id=source_id,
        ).parent / "manifest.json"

    def chapter_markdown_path(self, *, book_name: str, author: str, chapter_index: int, title: str) -> Path:
        return self.chapters_dir(book_name=book_name, author=author) / _chapter_file_name(chapter_index, title)

    def chapter_media_path(self, *, book_name: str, author: str, chapter_index: int, title: str) -> Path:
        """Descriptor JSON path for one audiobook/video chapter."""
        safe_title = _safe_segment(title or "untitled-chapter", max_length=120)
        return self.chapters_dir(book_name=book_name, author=author) / f"{chapter_index:04d}-{safe_title}.json"

    def write_chapter_media_file(self, *, path: Path, descriptor: dict[str, Any]) -> None:
        """Persist one media chapter descriptor (atomic JSON write)."""
        self.atomic_write_json(path, descriptor)

    def runtime_dir(self, *, book_name: str, author: str) -> Path:
        return self.private_root / _book_folder_name(book_name, author) / "runtime"

    def lock_runtime_dir(self, aggregate_book_id: str) -> Path:
        return self.private_root / "_locks" / _safe_segment(aggregate_book_id) / "runtime"

    def logs_dir(self, *, book_name: str, author: str) -> Path:
        return self.private_root / _book_folder_name(book_name, author) / "logs"

    def source_refs_path(self, *, book_name: str, author: str) -> Path:
        return self.private_root / _book_folder_name(book_name, author) / "source_refs.json"

    def build_shared_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build API-safe shared metadata without private source URLs."""
        data = dict(payload or {})
        return {
            "candidateId": data.get("candidateId", "") or "",
            "name": data.get("name", "") or "",
            "author": data.get("author", "") or "",
            "coverUrl": data.get("coverUrl", "") or "",
            "intro": data.get("intro", "") or "",
            "bookStatus": data.get("bookStatus", "") or "",
            "wordCount": data.get("wordCount", "") or "",
            "contentType": str(data.get("contentType", "") or "text"),
            "totalChaptersAtSubscribe": int(data.get("totalChaptersAtSubscribe", 0) or 0),
            "primaryBookId": data.get("primaryBookId", "") or "",
            "primarySourceId": data.get("primarySourceId", "") or "",
            "primarySourceName": data.get("primarySourceName", "") or "",
            "sourceMapSummary": self._build_shared_source_map_summary(data.get("sources")),
            "bookState": self.build_book_state_summary(data.get("bookState")),
        }

    def build_private_source_refs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build private source references with full source URLs."""
        data = dict(payload or {})
        source_rows = self._build_private_source_rows(data.get("sources"))
        primary_source_id = data.get("primarySourceId", "") or ""
        primary_book_id = data.get("primaryBookId", "") or ""
        primary_row = next(
            (
                row for row in source_rows
                if row.get("sourceId", "") == primary_source_id and row.get("bookId", "") == primary_book_id
            ),
            None,
        )
        return {
            "primary": {
                "sourceId": primary_source_id,
                "bookId": primary_book_id,
                "bookUrl": data.get("primaryBookUrl", "") or (primary_row or {}).get("bookUrl", ""),
                "tocUrl": data.get("primaryTocUrl", "") or (primary_row or {}).get("tocUrl", ""),
            },
            "sources": source_rows,
        }

    def atomic_write_json(self, path: Path, payload: dict[str, Any] | list[Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write_text(path, text)

    def atomic_write_markdown(self, path: Path, text: str) -> None:
        normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.endswith("\n"):
            normalized = f"{normalized}\n"
        self._atomic_write_text(path, normalized)

    def write_chapter_file(self, *, path: Path, title: str, body: str, trace_payload: dict[str, Any]) -> None:
        """Write a single chapter .md file with heading and trace block."""
        markdown = self.render_chapter_markdown(title=title, body=body, trace_payload=trace_payload)
        self.atomic_write_markdown(path, markdown)

    def update_chapter_index_entry(
        self,
        *,
        chapter_index_path: Path,
        metadata_path: Path,
        metadata_payload: dict[str, Any],
        entry: dict[str, Any],
        chapter_trace: dict[str, Any],
    ) -> dict[str, Any]:
        """Update one chapter entry and refresh metadata counts."""
        payload = self._read_json(chapter_index_path) or {}
        chapters = payload.get("chapters")
        if not isinstance(chapters, list):
            chapters = []

        chapter_index = int(entry.get("index", 0) or 0)
        replaced = False
        updated_chapters: list[dict[str, Any]] = []
        for item in chapters:
            if not isinstance(item, dict):
                continue
            if int(item.get("index", 0) or 0) == chapter_index:
                updated_chapters.append(self._normalize_chapter_index_entry({**item, **entry}))
                replaced = True
            else:
                updated_chapters.append(self._normalize_chapter_index_entry(item))
        if not replaced:
            updated_chapters.append(self._normalize_chapter_index_entry(entry))
        updated_chapters.sort(key=lambda item: int(item.get("index", 0) or 0))

        payload["schemaVersion"] = 2
        payload["chapters"] = updated_chapters
        if "bookId" not in payload and metadata_payload.get("candidateId"):
            payload["bookId"] = metadata_payload.get("candidateId", "")

        chapter_traces = {chapter_index: chapter_trace}
        book_dir = chapter_index_path.parent
        for item in updated_chapters:
            if not isinstance(item, dict):
                continue
            item_index = int(item.get("index", 0) or 0)
            if item_index <= 0 or item_index == chapter_index:
                continue
            file_name = str(item.get("file", "") or "").strip()
            if not file_name:
                continue
            try:
                chapter_traces[item_index] = self.parse_trace_block((book_dir / file_name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

        self.atomic_write_json(chapter_index_path, payload)
        rebuilt_metadata = self.rebuild_metadata_summary(
            metadata_payload,
            chapter_index_payload=payload,
            chapter_traces=chapter_traces,
        )
        self.atomic_write_json(metadata_path, rebuilt_metadata)
        return rebuilt_metadata

    def _normalize_chapter_index_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(entry)
        normalized.setdefault("isVip", False)
        normalized.setdefault("officialWordCount", 0)
        normalized.setdefault("officialPreviewWords", None)
        normalized.setdefault("sourceId", "")
        normalized.setdefault("sourceChapterId", "")
        normalized.setdefault("alignedWith", "")
        normalized.setdefault("alignmentScore", None)
        return normalized

    def render_trace_block(self, trace_payload: dict[str, Any]) -> str:
        payload = json.dumps(trace_payload or {}, ensure_ascii=False, indent=2)
        return f"<!-- {TRACE_BEGIN}\n{payload}\n{TRACE_END} -->"

    def render_chapter_markdown(self, *, title: str, body: str, trace_payload: dict[str, Any]) -> str:
        normalized_body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        heading = str(title or "").strip() or "未命名章节"
        trace_block = self.render_trace_block(trace_payload)
        if normalized_body.startswith(f"# {heading}"):
            content_body = normalized_body
        elif normalized_body:
            content_body = f"# {heading}\n\n{normalized_body}"
        else:
            content_body = f"# {heading}"
        return f"{content_body}\n\n{trace_block}\n"

    def parse_trace_block(self, markdown_text: str) -> dict[str, Any]:
        text = str(markdown_text or "")
        begin_index = text.find(TRACE_BEGIN)
        if begin_index < 0:
            raise ValueError("trace block markers not found")

        payload_start = begin_index + len(TRACE_BEGIN)
        end_index = text.find(TRACE_END, payload_start)
        if end_index < 0:
            raise ValueError("trace block markers not found")

        raw_payload = text[payload_start:end_index].strip()
        if raw_payload.startswith("-->"):
            raw_payload = raw_payload[3:].lstrip()
        if raw_payload.endswith("<!--"):
            raw_payload = raw_payload[:-4].rstrip()

        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"trace block contains invalid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError(f"trace block must decode to a JSON object, got {type(payload).__name__}")
        return payload

    def build_book_state_summary(self, raw_state: Any, *, chapter_count: int | None = None) -> dict[str, Any]:
        """Return a normalized shared metadata bookState summary."""
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        summary = {
            "status": state.get("status", "") or "",
            "searchVisibilityStatus": state.get("searchVisibilityStatus", "") or "",
            "chapterCount": int(state.get("chapterCount", 0) or 0),
            "processedChapterCount": int(state.get("processedChapterCount", 0) or 0),
            "readableChapterCount": int(state.get("readableChapterCount", 0) or 0),
            "previewChapterCount": int(state.get("previewChapterCount", 0) or 0),
            "proofreadCompleteCount": int(state.get("proofreadCompleteCount", 0) or 0),
            "suspectChapterCount": int(state.get("suspectChapterCount", 0) or 0),
            "failedChapterCount": int(state.get("failedChapterCount", 0) or 0),
            "sourceSnapshotChapterCount": int(state.get("sourceSnapshotChapterCount", 0) or 0),
            "sourceSnapshotSourceCount": int(state.get("sourceSnapshotSourceCount", 0) or 0),
            "sourceSnapshotFailedCount": int(state.get("sourceSnapshotFailedCount", 0) or 0),
            "latestChapterIndex": int(state.get("latestChapterIndex", 0) or 0),
            "latestChapterTitle": state.get("latestChapterTitle", "") or "",
            "lastUpdateCheckAt": state.get("lastUpdateCheckAt", "") or "",
        }
        if chapter_count is not None:
            summary["chapterCount"] = int(chapter_count or 0)
        return summary

    def normalize_chapter_status(
        self,
        chapter_entry: dict[str, Any] | None = None,
        chapter_trace: dict[str, Any] | None = None,
    ) -> str:
        """Resolve the normalized chapter status from index first, trace second."""
        index_status = str((chapter_entry or {}).get("status", "") or "").strip().lower()
        if index_status:
            return index_status
        trace_status = str((chapter_trace or {}).get("chapterStatus", "") or "").strip().lower()
        if trace_status:
            return trace_status
        return ""

    def rebuild_book_state_from_files(
        self,
        *,
        chapter_index_payload: dict[str, Any] | None,
        chapter_traces: dict[int, dict[str, Any]] | None = None,
        existing_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recalculate derived counts from chapter_index.json plus chapter traces."""
        chapter_index_payload = chapter_index_payload if isinstance(chapter_index_payload, dict) else {}
        chapter_traces = chapter_traces if isinstance(chapter_traces, dict) else {}
        existing_state = existing_state if isinstance(existing_state, dict) else {}
        chapter_entries = chapter_index_payload.get("chapters")
        if not isinstance(chapter_entries, list):
            chapter_entries = []

        processed_count = 0
        readable_count = 0
        preview_count = 0
        proofread_complete_count = 0
        suspect_count = 0
        failed_count = 0
        latest_index = 0
        latest_title = ""

        for item in chapter_entries:
            if not isinstance(item, dict):
                continue
            chapter_index = int(item.get("index", 0) or 0)
            title = item.get("title", "") or ""
            status = self.normalize_chapter_status(item, chapter_traces.get(chapter_index))

            if chapter_index >= latest_index:
                latest_index = chapter_index
                latest_title = title
            if status in CHAPTER_STATUS_PROCESSED:
                processed_count += 1
            if status in CHAPTER_STATUS_READABLE:
                readable_count += 1
            if status in CHAPTER_STATUS_FAILED:
                failed_count += 1
            if status == "proofread_complete":
                proofread_complete_count += 1
            if status == "suspect":
                suspect_count += 1

            trace = chapter_traces.get(chapter_index)
            preview_only = bool((trace or {}).get("previewOnly"))
            if status in CHAPTER_STATUS_PREVIEW or preview_only:
                preview_count += 1

        return self.build_book_state_summary(
            {
                **existing_state,
                "chapterCount": len([item for item in chapter_entries if isinstance(item, dict)]),
                "processedChapterCount": processed_count,
                "readableChapterCount": readable_count,
                "previewChapterCount": preview_count,
                "proofreadCompleteCount": proofread_complete_count,
                "suspectChapterCount": suspect_count,
                "failedChapterCount": failed_count,
                "latestChapterIndex": latest_index,
                "latestChapterTitle": latest_title,
            }
        )

    def book_state_needs_rebuild(
        self,
        *,
        metadata_payload: dict[str, Any] | None,
        chapter_index_payload: dict[str, Any] | None,
        chapter_traces: dict[int, dict[str, Any]] | None = None,
    ) -> bool:
        metadata_payload = metadata_payload if isinstance(metadata_payload, dict) else {}
        expected = self.rebuild_book_state_from_files(
            chapter_index_payload=chapter_index_payload,
            chapter_traces=chapter_traces,
            existing_state=metadata_payload.get("bookState"),
        )
        current = self.build_book_state_summary(
            metadata_payload.get("bookState"),
            chapter_count=expected["chapterCount"],
        )
        for key in (
            "chapterCount",
            "processedChapterCount",
            "readableChapterCount",
            "previewChapterCount",
            "proofreadCompleteCount",
            "suspectChapterCount",
            "failedChapterCount",
            "latestChapterIndex",
            "latestChapterTitle",
        ):
            if current.get(key) != expected.get(key):
                return True
        return False

    def update_free_chapter_end_index(
        self, *, book_name: str, author: str, free_chapter_end_index: int
    ) -> None:
        """更新 metadata.json 中的 freeChapterEndIndex 字段（书级元数据）。

        如果 metadata.json 不存在则跳过（等章节处理时创建）。
        """
        path = self.metadata_path(book_name=book_name, author=author)
        if not path.exists():
            return
        payload = self._read_json(path) or {}
        payload["freeChapterEndIndex"] = int(free_chapter_end_index)
        self.atomic_write_json(path, payload)

    def rebuild_metadata_summary(
        self,
        metadata_payload: dict[str, Any] | None,
        *,
        chapter_index_payload: dict[str, Any] | None,
        chapter_traces: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        metadata_payload = dict(metadata_payload or {})
        metadata_payload["bookState"] = self.rebuild_book_state_from_files(
            chapter_index_payload=chapter_index_payload,
            chapter_traces=chapter_traces,
            existing_state=metadata_payload.get("bookState"),
        )
        return metadata_payload

    def write_book_bundle(
        self,
        *,
        metadata_path: Path,
        metadata_payload: dict[str, Any],
        chapter_index_path: Path,
        chapter_index_payload: dict[str, Any],
        chapter_files: list[tuple[Path, str]],
    ) -> dict[str, Any]:
        """Persist chapter files, then chapter_index.json, then metadata.json."""
        chapter_traces: dict[int, dict[str, Any]] = {}
        for path, markdown in chapter_files:
            self.atomic_write_markdown(path, markdown)
            try:
                trace = self.parse_trace_block(markdown)
            except ValueError:
                continue
            chapter_index = int(trace.get("chapterIndex", 0) or 0)
            if chapter_index > 0:
                chapter_traces[chapter_index] = trace

        self.atomic_write_json(chapter_index_path, chapter_index_payload)
        rebuilt_metadata = self.rebuild_metadata_summary(
            metadata_payload,
            chapter_index_payload=chapter_index_payload,
            chapter_traces=chapter_traces,
        )
        self.atomic_write_json(metadata_path, rebuilt_metadata)
        return rebuilt_metadata

    def _build_shared_source_map_summary(self, raw_sources: Any) -> list[dict[str, Any]]:
        """Build API-safe source summary without private source URLs or book IDs.

        The ``bookId`` value produced by the id codec embeds the source book URL,
        so it must not be included in the shared metadata that may be served to
        anonymous readers.
        """
        if not isinstance(raw_sources, list):
            return []
        summary: list[dict[str, Any]] = []
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            summary.append(
                {
                    "sourceId": item.get("sourceId", "") or "",
                    "sourceName": item.get("sourceName", "") or "",
                    "score": int(item.get("score", 0) or 0),
                    "lastChapter": item.get("lastChapter", "") or "",
                    "chapterCount": int(item.get("chapterCount", 0) or 0),
                    "bookStatus": item.get("bookStatus", "") or "",
                    "author": item.get("author", "") or "",
                    "name": item.get("name", "") or "",
                }
            )
        return summary

    def _build_private_source_rows(self, raw_sources: Any) -> list[dict[str, str]]:
        if not isinstance(raw_sources, list):
            return []
        rows: list[dict[str, str]] = []
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            source_id = item.get("sourceId", "") or ""
            book_id = item.get("bookId", "") or ""
            if not source_id or not book_id:
                continue
            rows.append(
                {
                    "sourceId": source_id,
                    "sourceName": item.get("sourceName", "") or "",
                    "bookId": book_id,
                    "bookUrl": item.get("bookUrl", "") or "",
                    "tocUrl": item.get("tocUrl", "") or "",
                }
            )
        return rows

    def _atomic_write_text(self, path: Path, text: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")

        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_path, target)

    def cleanup_tmp_files(self, *, book_name: str, author: str) -> list[Path]:
        """Remove leftover *.tmp files under the shared book directory.

        Returns the list of removed paths.
        """
        removed: list[Path] = []
        book_dir = self.shared_book_dir(book_name=book_name, author=author)
        if not book_dir.exists():
            return removed
        for tmp_path in book_dir.rglob("*.tmp"):
            try:
                tmp_path.unlink()
                removed.append(tmp_path)
            except OSError:
                pass
        return removed

    def check_chapter_traces(
        self,
        *,
        book_name: str,
        author: str,
        chapter_index_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate that every indexed chapter has a parseable trace block.

        Returns {"valid": bool, "total": int, "broken": list[int], "missing": list[int]}
        """
        if chapter_index_payload is None:
            chapter_index_path = self.chapter_index_path(book_name=book_name, author=author)
            chapter_index_payload = self._read_json(chapter_index_path) or {}

        entries = chapter_index_payload.get("chapters", []) if isinstance(chapter_index_payload, dict) else []
        broken: list[int] = []
        missing: list[int] = []
        valid_count = 0
        book_dir = self.shared_book_dir(book_name=book_name, author=author)

        for item in entries:
            if not isinstance(item, dict):
                continue
            chapter_index = int(item.get("index", 0) or 0)
            file_name = str(item.get("file", "") or "").strip()
            chapter_path = book_dir / file_name if file_name else None
            if chapter_path is None or not chapter_path.exists():
                missing.append(chapter_index)
                continue
            # Media chapter descriptors are JSON (trace lives inside the file).
            if chapter_path.suffix.lower() == ".json":
                valid_count += 1
                continue
            try:
                self.parse_trace_block(chapter_path.read_text(encoding="utf-8"))
                valid_count += 1
            except ValueError:
                broken.append(chapter_index)

        return {
            "valid": len(broken) == 0 and len(missing) == 0,
            "total": len([item for item in entries if isinstance(item, dict)]),
            "broken": broken,
            "missing": missing,
        }

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError(f"JSON file must contain an object: {path}")
        return payload
