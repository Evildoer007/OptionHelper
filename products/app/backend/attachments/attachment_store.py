"""Unified attachment boundary for OptionHelper conversations."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ValidationError
from .document_store import DocumentAttachmentStore
from .image_store import ImageAttachmentStore


class AttachmentStore:
    max_files_per_message = 20
    max_message_source_bytes = 200 * 1024 * 1024
    max_storage_bytes = 2 * 1024 * 1024 * 1024
    # A content hash can be reused by an upload that has saved the bytes but
    # has not yet registered its Task draft. Keep recently saved content out
    # of collection so that this short cross-store handoff remains safe.
    orphan_retention_seconds = 15 * 60

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self.images = ImageAttachmentStore(self._root / "images")
        self.documents = DocumentAttachmentStore(self._root / "documents")

    def save_encoded(self, files: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if isinstance(files, (str, bytes)) or not isinstance(files, Sequence):
            raise ValidationError("附件必须是数组")
        if not files or len(files) > self.max_files_per_message:
            raise ValidationError(f"每条消息最多添加{self.max_files_per_message}个附件")
        total = 0
        images: list[tuple[int, Mapping[str, Any]]] = []
        documents: list[tuple[int, Mapping[str, Any]]] = []
        for index, item in enumerate(files):
            if not isinstance(item, Mapping):
                raise ValidationError("附件字段无效")
            encoded = item.get("data")
            if not isinstance(encoded, str):
                raise ValidationError("附件缺少数据")
            total += len(encoded) * 3 // 4
            if total > self.max_message_source_bytes:
                raise ValidationError("本条消息的附件总量超过200MB上限")
            target = images if str(item.get("media_type", "")).startswith("image/") else documents
            target.append((index, item))
        if self._stored_bytes() + total > self.max_storage_bytes:
            raise ValidationError("OptionHelper附件存储已达到2GB上限，暂不能继续上传")
        result: list[dict[str, Any] | None] = [None] * len(files)
        if images:
            saved = self.images.save_encoded([item for _index, item in images])
            for (index, _item), reference in zip(images, saved, strict=True):
                result[index] = reference
        if documents:
            saved = self.documents.save_encoded([item for _index, item in documents])
            for (index, _item), reference in zip(documents, saved, strict=True):
                result[index] = reference
        references = [dict(item) for item in result if item is not None]
        for reference in references:
            self._touch_artifact(reference)
        return references

    def collect_orphans(self, referenced_attachment_ids: set[str]) -> dict[str, int]:
        """Reclaim complete, unreferenced content-addressed attachment groups.

        Callers supply the complete live Task reference set. Invalid,
        incomplete, recently used, and unverifiable groups are retained so a
        cleanup pass can never turn uncertain storage into data loss.
        """

        protected = {
            attachment_id
            for attachment_id in referenced_attachment_ids
            if isinstance(attachment_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", attachment_id)
        }
        summary = {
            "scanned": 0,
            "referenced": 0,
            "recent": 0,
            "retained_invalid": 0,
            "reclaimed": 0,
            "reclaimed_bytes": 0,
        }
        cutoff = time.time() - self.orphan_retention_seconds
        for reference, paths in self._complete_artifacts():
            summary["scanned"] += 1
            attachment_id = str(reference["attachment_id"])
            if attachment_id in protected:
                summary["referenced"] += 1
                continue
            try:
                if max(path.stat().st_mtime for path in paths) > cutoff:
                    summary["recent"] += 1
                    continue
                self.read(reference)
                reclaimed_bytes = sum(path.stat().st_size for path in paths)
            except (OSError, ValueError):
                summary["retained_invalid"] += 1
                continue
            try:
                for path in paths:
                    path.unlink()
            except OSError:
                summary["retained_invalid"] += 1
                continue
            summary["reclaimed"] += 1
            summary["reclaimed_bytes"] += reclaimed_bytes
        return summary

    def _complete_artifacts(self) -> list[tuple[dict[str, Any], tuple[Path, ...]]]:
        result: list[tuple[dict[str, Any], tuple[Path, ...]]] = []
        layouts = (
            ("images", "image", ("image", "json")),
            ("documents", "document", ("document", "txt", "json")),
        )
        for directory_name, kind, suffixes in layouts:
            version_root = self._root / directory_name / "v1"
            if version_root.is_symlink() or not version_root.is_dir():
                continue
            try:
                shards = tuple(version_root.iterdir())
            except OSError:
                continue
            for shard in shards:
                if shard.is_symlink() or not shard.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                    continue
                try:
                    entries = tuple(shard.iterdir())
                except OSError:
                    continue
                for data_path in entries:
                    match = re.fullmatch(r"([0-9a-f]{64})\." + suffixes[0], data_path.name)
                    if match is None:
                        continue
                    digest = match.group(1)
                    paths = tuple(shard / f"{digest}.{suffix}" for suffix in suffixes)
                    if any(path.is_symlink() or not path.is_file() for path in paths):
                        continue
                    metadata_path = paths[-1]
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(metadata, dict) or metadata.get("attachment_id") != f"sha256:{digest}" or metadata.get("kind") != kind:
                        continue
                    result.append((metadata, paths))
        return result

    def _touch_artifact(self, reference: Mapping[str, Any]) -> None:
        try:
            digest = str(reference["attachment_id"]).removeprefix("sha256:")
            if reference.get("kind") == "image":
                paths = self.images._paths(digest)
            elif reference.get("kind") == "document":
                paths = self.documents._paths(digest)
            else:
                return
            if any(path.is_symlink() or not path.is_file() for path in paths):
                return
            now = time.time()
            for path in paths:
                os.utime(path, (now, now), follow_symlinks=False)
        except (KeyError, OSError, ValueError):
            # The store's own commit/read validation is authoritative. A
            # failed best-effort timestamp refresh must never reject an upload.
            return

    def _stored_bytes(self) -> int:
        total = 0
        if not self._root.exists():
            return 0
        for path in self._root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total > self.max_storage_bytes:
                break
        return total

    def read(self, reference: Mapping[str, Any]) -> bytes:
        return self.images.read(reference) if reference.get("kind") == "image" else self.documents.read(reference)

    def text(self, reference: Mapping[str, Any]) -> str:
        if reference.get("kind") != "document":
            raise ValidationError("图片附件没有文档文本索引")
        return self.documents.extracted_text(reference)

    def search(self, reference: Mapping[str, Any], query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        return self.documents.search(reference, query, limit=limit)

    def read_chunk(self, reference: Mapping[str, Any], index: int) -> dict[str, Any]:
        return self.documents.read_chunk(reference, index)
