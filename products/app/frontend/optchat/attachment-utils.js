const mediaTypeByExtension = Object.freeze({
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
    if (!attachmentMediaType(file)) return `不支持“${name}”，请添加图片、PDF、DOCX、XLSX/XLS、PPTX、RTF、MD、TXT、CSV/TSV、HTML、JSON、XML、YAML或ODT/ODS/ODP。旧版DOC/PPT请先另存为DOCX/PPTX。`;
    if (!Number.isFinite(file?.size) || file.size <= 0) return `“${name}”为空文件。`;
    if (file.size > ATTACHMENT_LIMITS.maxFileBytes) return `“${name}”超过20MB上限。`;
    total += file.size;
  }
  if (total > ATTACHMENT_LIMITS.maxTotalBytes) return "本条消息附件总量不能超过200MB。";
  return "";
}
