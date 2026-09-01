"""Content-addressed image storage owned by the OptionHelper App.

The session log stores only immutable references and verified metadata. Browser
object URLs, source paths and base64 payloads never become conversation state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageOps, UnidentifiedImageError

from ..errors import ValidationError


_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_FORMAT_MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
_ATTACHMENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ImageAttachmentStore:
    """Validate, normalize and atomically store immutable raster images."""

    max_images_per_message = 20
    max_source_bytes = 20 * 1024 * 1024
    max_message_source_bytes = 200 * 1024 * 1024
    max_source_pixels = 64_000_000
    max_source_dimension = 8_192
    max_stored_dimension = 2_048
    max_stored_bytes = 4 * 1024 * 1024

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve() / "v1"
        self._mkdir_private(self._root)

    def save_encoded(self, images: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if isinstance(images, (str, bytes)) or not isinstance(images, Sequence):
            raise ValidationError("图片附件必须是数组")
        if not images or len(images) > self.max_images_per_message:
            raise ValidationError(f"每条消息最多添加{self.max_images_per_message}张图片")
        decoded: list[tuple[bytes, str, str | None]] = []
        total = 0
        for item in images:
            if not isinstance(item, Mapping) or set(item).difference({"data", "media_type", "name"}):
                raise ValidationError("图片附件字段无效")
            media_type = str(item.get("media_type", "")).strip().lower()
            if media_type not in _MEDIA_TYPES:
                raise ValidationError("仅支持PNG、JPEG、WebP和GIF图片")
            encoded = item.get("data")
            if not isinstance(encoded, str) or not encoded:
                raise ValidationError("图片附件缺少数据")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ValidationError("图片附件不是有效的Base64数据") from None
            if not data or len(data) > self.max_source_bytes:
                raise ValidationError("单张图片超过20MB上限")
            total += len(data)
            if total > self.max_message_source_bytes:
                raise ValidationError("本条消息的图片总量超过200MB上限")
            decoded.append((data, media_type, self._safe_name(item.get("name"))))

        prepared = [self._prepare(data, media_type, name) for data, media_type, name in decoded]
        return [self._commit(data, metadata) for data, metadata in prepared]

    def read(self, reference: Mapping[str, Any]) -> bytes:
        attachment_id = str(reference.get("attachment_id", ""))
        digest = self._digest(attachment_id)
        data_path, metadata_path = self._paths(digest)
        self._reject_link(data_path)
        self._reject_link(metadata_path)
        try:
            data = data_path.read_bytes()
            stored = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValidationError("图片附件不可用") from None
        if hashlib.sha256(data).hexdigest() != digest or stored != dict(reference):
            raise ValidationError("图片附件完整性校验失败")
        self._verify_stored(data, stored)
        return data

    def _prepare(self, source: bytes, declared_media: str, name: str | None) -> tuple[bytes, dict[str, Any]]:
        try:
            with Image.open(io.BytesIO(source)) as opened:
                actual_media = _FORMAT_MEDIA.get(str(opened.format or "").upper())
                if actual_media != declared_media:
                    raise ValidationError("图片声明格式与实际内容不一致")
                original_width, original_height = opened.size
                if min(original_width, original_height) < 1:
                    raise ValidationError("图片尺寸无效")
                if original_width * original_height > self.max_source_pixels:
                    raise ValidationError("图片像素数量超过上限")
                if max(original_width, original_height) > self.max_source_dimension:
                    raise ValidationError("图片边长超过8192像素上限")
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                normalized = oriented.copy()
        except ValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise ValidationError("图片内容无法解码") from None

        reduced = max(normalized.size) > self.max_stored_dimension
        if reduced:
            normalized.thumbnail((self.max_stored_dimension, self.max_stored_dimension), Image.Resampling.LANCZOS)
        data, media_type = self._encode(normalized)
        if len(data) > self.max_stored_bytes:
            raise ValidationError("图片规范化后仍超过4MB上限")
        width, height = normalized.size
        digest = hashlib.sha256(data).hexdigest()
        metadata: dict[str, Any] = {
            "attachment_id": f"sha256:{digest}",
            "kind": "image",
            "media_type": media_type,
            "bytes": len(data),
            "width": width,
            "height": height,
        }
        if name:
            metadata["name"] = name
        if reduced:
            metadata["original_dimensions"] = {"width": original_width, "height": original_height}
        return data, metadata

    def _encode(self, image: Image.Image) -> tuple[bytes, str]:
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        if has_alpha:
            converted = image.convert("RGBA")
            output = io.BytesIO()
            converted.save(output, format="WEBP", lossless=True, method=6)
            if output.tell() <= self.max_stored_bytes:
                return output.getvalue(), "image/webp"
        converted = image.convert("RGB")
        for quality in (90, 82, 74, 66, 58):
            output = io.BytesIO()
            converted.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
            if output.tell() <= self.max_stored_bytes:
                return output.getvalue(), "image/jpeg"
        return output.getvalue(), "image/jpeg"

    def _commit(self, data: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        digest = self._digest(str(metadata["attachment_id"]))
        data_path, metadata_path = self._paths(digest)
        self._mkdir_private(data_path.parent)
        for path in (data_path, metadata_path):
            self._reject_link(path)
        if data_path.exists() and metadata_path.exists():
            self.read(metadata)
            return dict(metadata)
        self._atomic_write(data_path, data)
        self._atomic_write(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        self.read(metadata)
        return dict(metadata)

    def _verify_stored(self, data: bytes, metadata: Mapping[str, Any]) -> None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                media_type = _FORMAT_MEDIA.get(str(image.format or "").upper())
                if metadata.get("kind", "image") != "image" or media_type != metadata.get("media_type") or list(image.size) != [metadata.get("width"), metadata.get("height")]:
                    raise ValidationError("图片附件元数据校验失败")
        except ValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError):
            raise ValidationError("图片附件无法重新验证") from None

    def _paths(self, digest: str) -> tuple[Path, Path]:
        directory = self._root / digest[:2]
        return directory / f"{digest}.image", directory / f"{digest}.json"

    @staticmethod
    def _safe_name(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        name = Path(value.replace("\\", "/")).name.strip()
        name = "".join(char for char in name if char.isprintable())[:160]
        return name or None

    @staticmethod
    def _digest(attachment_id: str) -> str:
        if not _ATTACHMENT_ID.fullmatch(attachment_id):
            raise ValidationError("图片附件引用无效")
        return attachment_id.removeprefix("sha256:")

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValidationError("图片附件目录不能是符号链接")
        os.chmod(path, 0o700)

    @staticmethod
    def _reject_link(path: Path) -> None:
        if path.is_symlink():
            raise ValidationError("图片附件不能使用符号链接")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
