// Videos stay local. Only bounded, timestamped stills and a sampling note
// enter the existing attachment store; no audio transcription is implied.
import { attachmentMediaType, ATTACHMENT_LIMITS } from './attachment-utils.js';

function mediaEvent(video, name, action) {
  return new Promise((resolve, reject) => {
    const finish = (error) => {
      clearTimeout(timer);
      video.removeEventListener(name, ready);
      video.removeEventListener('error', failed);
      error ? reject(error) : resolve();
    };
    const ready = () => finish();
    const failed = () => finish(new Error('无法解码这段视频，请转为H.264编码的MP4后添加。'));
    const timer = setTimeout(() => finish(new Error('视频读取超时，请缩短录屏或转换为MP4后重试。')), 15000);
    video.addEventListener(name, ready, { once: true });
    video.addEventListener('error', failed, { once: true });
    try { action(); } catch (error) { finish(error); }
  });
}

export function videoSampleTimes(duration) {
  if (!Number.isFinite(duration) || duration <= 0) throw new Error('无法读取视频时长。');
  const count = Math.min(4, Math.max(1, Math.ceil(duration / 5)));
  return Array.from({ length: count }, (_, i) => duration * (i + .5) / count);
}

async function sampleVideo(file) {
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.preload = 'auto';
  const url = URL.createObjectURL(file);
  try {
    await mediaEvent(video, 'loadeddata', () => { video.src = url; video.load(); });
    const times = videoSampleTimes(video.duration);
    if (!video.videoWidth || !video.videoHeight) throw new Error('视频没有可读取的画面。');
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 1600 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new Error('无法创建视频画面，请重新打开App后重试。');
    const files = [];
    const frameHashes = new Set();
    const sourceName = file.name.slice(0, 90);
    for (const time of times) {
      await mediaEvent(video, 'seeked', () => { video.currentTime = time; });
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .86));
      if (!blob) throw new Error('视频画面导出失败。');
      const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
      const hash = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
      if (frameHashes.has(hash)) continue;
      frameHashes.add(hash);
      files.push(new File([blob], `${sourceName} ${time.toFixed(2)}秒.jpg`, { type: 'image/jpeg' }));
    }
    const note = `视频抽帧说明\n来源：${file.name}\n时长：${video.duration.toFixed(2)}秒\n采样时点：${times.map(t => t.toFixed(2) + '秒').join('、')}\n在上述${times.length}个时间点采样，完全相同的画面已合并，保留${files.length}帧。未解析完整视频或音频。画面间发生的内容可能遗漏，不得推断为已完整观看。`;
    files.push(new File([note], `${sourceName} 抽帧说明.txt`, { type: 'text/plain' }));
    return files;
  } finally {
    video.pause();
    video.removeAttribute('src');
    video.load();
    URL.revokeObjectURL(url);
  }
}

export async function prepareAttachmentFiles(files, existingCount = 0) {
  const prepared = [];
  for (const file of files) {
    const additions = attachmentMediaType(file).startsWith('video/') ? await sampleVideo(file) : [file];
    if (existingCount + prepared.length + additions.length > ATTACHMENT_LIMITS.maxFiles) {
      throw new Error('视频抽帧后附件超过20个，请分次添加或减少文件。');
    }
    prepared.push(...additions);
  }
  return prepared;
}
