const mediaTypeByExtension = Object.freeze({
  ".mov": "video/quicktime",
  ".mp4": "video/mp4",
  ".m4v": "video/mp4",
  ".webm": "video/webm",
  ".bmp": "image/bmp",
  ".tif": "image/tiff",
  ".tiff": "image/tiff",
  ".eml": "message/rfc822",
  ".py": "text/plain",
  ".js": "text/plain",
  ".ts": "text/plain",
  ".jsx": "text/plain",
  ".tsx": "text/plain",
  ".sql": "text/plain",
  ".r": "text/plain",
  ".css": "text/plain",
  ".sh": "text/plain",
  ".toml": "text/plain",
  ".ini": "text/plain",
  ".srt": "text/plain",
  ".vtt": "text/plain",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".csv": "text/csv",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".rtf": "application/rtf",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".xls": "application/vnd.ms-excel",
  ".tsv": "text/tab-separated-values",
  ".html": "text/html",
  ".htm": "text/html",
  ".json": "application/json",
  ".jsonl": "application/json",
  ".xml": "application/xml",
  ".yaml": "application/yaml",
  ".yml": "application/yaml",
  ".log": "text/plain",
  ".odt": "application/vnd.oasis.opendocument.text",
  ".ods": "application/vnd.oasis.opendocument.spreadsheet",
  ".odp": "application/vnd.oasis.opendocument.presentation",
});

export const ATTACHMENT_ACCEPT = Object.keys(mediaTypeByExtension).join(",");

export const ATTACHMENT_LIMITS = Object.freeze({
  maxFiles: 20,
  maxFileBytes: 20 * 1024 * 1024,
  maxVideoBytes: 100 * 1024 * 1024,
  maxTotalBytes: 200 * 1024 * 1024,
});

export function attachmentMediaType(file) {
  // Native pickers disagree on generic MIME types; the backend verifies the
  // extension and actually parses the bytes before a file becomes usable.
  const name = String(file?.name || "").toLowerCase();
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";
  return mediaTypeByExtension[extension] || "";
}

export function validateAttachments(files, { existingCount = 0, existingBytes = 0 } = {}) {
  const list = Array.from(files || []);
  if (existingCount + list.length > ATTACHMENT_LIMITS.maxFiles) {
    return `每条消息最多添加${ATTACHMENT_LIMITS.maxFiles}个附件。`;
  }
  let total = Number(existingBytes) || 0;
  for (const file of list) {
    const name = String(file?.name || "未命名文件");
    const mediaType = attachmentMediaType(file);
    if (!mediaType) return `暂不支持${name}的格式，请转换为PDF、图片或文本后添加。`;
    if (!Number.isFinite(file?.size) || file.size <= 0) return `${name}为空文件。`;
    const limit = mediaType.startsWith("video/") ? ATTACHMENT_LIMITS.maxVideoBytes : ATTACHMENT_LIMITS.maxFileBytes;
    if (file.size > limit) return `${name}超过${limit / 1024 / 1024}MB上限。`;
    total += file.size;
  }
  if (total > ATTACHMENT_LIMITS.maxTotalBytes) return "本条消息附件总量不能超过200MB。";
  return "";
}
