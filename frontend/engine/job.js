/* One conversion, start to finish, in this tab.
 *
 *   audio --ffmpeg--> two-minute chunks --whisper--> rough transcript
 *   book  ----------> paragraphs
 *   transcript + paragraphs --align--> subtitles (.srt)
 *   cover + audio (+ subtitles) --ffmpeg--> video, on request
 *
 * Nothing leaves the machine. The transcript is saved to IndexedDB after every
 * chunk, because transcribing a book takes hours and tabs get closed: opening
 * the same files again carries on from the last chunk, and aligning the same
 * audio against a different edition of the book costs seconds.
 */
import { syncedEpub } from './epub.js';
import { Media, quietestPoint, SAMPLE_RATE } from './media.js';
import { Recogniser } from './asr.js';
import { readBook } from './book.js';

const CHUNK_SECONDS = 120;

export class Cancelled extends Error { constructor() { super('Stopped.'); } }

/* ------------------------------------------------------------ saved progress */

const db = () => new Promise((resolve, reject) => {
  const open = indexedDB.open('subread', 1);
  open.onupgradeneeded = () => open.result.createObjectStore('transcripts');
  open.onsuccess = () => resolve(open.result);
  open.onerror = () => reject(open.error);
});

async function store(mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const tx = d.transaction('transcripts', mode);
    const req = fn(tx.objectStore('transcripts'));
    tx.oncomplete = () => resolve(req?.result);
    tx.onerror = () => reject(tx.error);
  });
}

/** The same files picked again resume, whatever route they were picked by. */
const keyFor = (files, language) => files.map((f) => `${f.name}|${f.size}`).join('//') + `#${language}`;

export async function savedProgress(files, language) {
  try { return (await store('readonly', (s) => s.get(keyFor(files, language)))) ?? null; } catch { return null; }
}

/* ------------------------------------------------------------------- the job */

export class Job {
  #media = null;
  #paths = [];
  #cancelled = false;
  #book = null;

  constructor({ audio, book, language, onStatus }) {
    this.audio = audio;          // File[], in playback order
    this.bookFile = book;        // File
    this.language = language;    // Whisper code; always explicit, see asr.js
    this.onStatus = onStatus ?? (() => {});
    this.result = null;
  }

  cancel() { this.#cancelled = true; }
  #check() { if (this.#cancelled) throw new Cancelled(); }
  #status(phase, detail, fraction, extra = {}) { this.onStatus({ phase, detail, fraction, ...extra }); }

  async run() {
    this.#status('preparing', 'Reading the book', 0);
    this.#book = await readBook(this.bookFile);
    if (!this.#book.paragraphs.length) throw new Error(`No text could be read from ${this.bookFile.name}.`);

    this.#status('preparing', 'Starting the audio decoder', 0);
    this.#media = await Media.open();
    const parts = [];
    for (const [i, file] of this.audio.entries()) {
      const path = await this.#media.mount(file, `/in${i}`);
      parts.push({ path, ...(await this.#media.probe(path)) });
      this.#paths.push(path);
    }
    const total = parts.reduce((n, p) => n + p.duration, 0);

    const transcript = await this.#transcribe(parts, total);
    this.#check();

    this.#status('aligning', 'Matching the book to the narration', 0.97);
    const aligned = await alignInWorker(transcript, this.#book.paragraphs, this.language);
    const stem = (this.audio.length > 1 ? this.bookFile : this.audio[0]).name.replace(/\.[^.]+$/, '');
    this.result = {
      ...aligned, stem, duration: total, parts,
      srtName: `${stem}.${this.language}.srt`,
      segments: transcript.length,
    };
    this.#status('done', 'Done', 1);
    return this.result;
  }

  async #transcribe(parts, total) {
    const key = keyFor(this.audio, this.language);
    const saved = (await savedProgress(this.audio, this.language)) ?? { doneUntil: 0, segments: [], complete: false };
    if (saved.complete) return saved.segments;

    this.#status('preparing', 'Loading the speech model', 0, { indeterminate: true });
    const asr = await Recogniser.open((p) => {
      if (p.total) this.#status('preparing', 'Downloading the speech model (once)', p.loaded / p.total * 0.02);
    });
    this.device = asr.device;

    const resumedAt = saved.doneUntil, started = performance.now();
    let base = 0;   // where the current part starts on the whole book's clock
    for (const part of parts) {
      let pos = Math.max(0, saved.doneUntil - base);
      while (pos < part.duration - 0.05) {
        this.#check();
        let pcm = await this.#media.pcm(part.path, pos, CHUNK_SECONDS);
        if (!pcm.length) break;
        const last = pos + pcm.length / SAMPLE_RATE >= part.duration - 0.5;
        // Cut where the narrator pauses, so no word straddles two chunks.
        if (!last) pcm = pcm.subarray(0, quietestPoint(pcm));

        const at = base + pos;
        saved.segments.push(...await asr.transcribe(pcm, this.language, at));
        pos += pcm.length / SAMPLE_RATE;
        saved.doneUntil = base + pos;
        await store('readwrite', (s) => s.put(saved, key)).catch(() => {});

        const elapsed = (performance.now() - started) / 1000;
        const speed = (saved.doneUntil - resumedAt) / elapsed;
        this.#status('transcribing', `Listening: ${clock(saved.doneUntil)} of ${clock(total)}`,
          0.02 + 0.94 * saved.doneUntil / total,
          { eta: elapsed > 10 ? (total - saved.doneUntil) / speed : null, speed: elapsed > 10 ? speed : null });
      }
      base += part.duration;
    }
    saved.complete = true;
    await store('readwrite', (s) => s.put(saved, key)).catch(() => {});
    return saved.segments;
  }

  /**
   * The video, made on request: a still image over the audio.
   * kind "mkv" carries the subtitles inside the file (MPV, VLC);
   * kind "mp4" is clean, for YouTube, where the .srt is uploaded alongside.
   *
   * A still picture does not need encoding for ten hours. One minute of it is
   * encoded once and then repeated by stream copy, so the cost is that of
   * copying the audio - seconds, not an hour.
   */
  async video(kind, { coverFile, onProgress } = {}) {
    const m = this.#media, r = this.result;
    const still = await canvasPng(coverFile ?? this.#book.cover);
    await m.write('canvas.png', still);
    await m.run(['-loop', '1', '-framerate', '1', '-i', 'canvas.png', '-t', '60',
      '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'stillimage', '-pix_fmt', 'yuv420p',
      '-r', '1', '-g', '60', 'still.mp4']);

    let audioIn;
    if (this.#paths.length === 1) audioIn = ['-i', this.#paths[0]];
    else {
      const list = this.#paths.map((p) => `file '${p.replaceAll("'", "'\\''")}'`).join('\n');
      await m.write('parts.txt', new TextEncoder().encode(list));
      audioIn = ['-f', 'concat', '-safe', '0', '-i', 'parts.txt'];
    }
    // Copy the audio when the container can hold it; re-encoding a whole book
    // in WebAssembly is the one slow thing here, so only when there is no choice.
    const copyable = kind === 'mkv'
      ? ['aac', 'mp3', 'opus', 'vorbis', 'flac', 'ac3', 'alac']
      : ['aac', 'mp3', 'ac3', 'alac'];
    const codecs = new Set(r.parts.map((p) => p.codec));
    const audioCodec = codecs.size === 1 && copyable.includes([...codecs][0]) ? ['-c:a', 'copy'] : ['-c:a', 'aac', '-b:a', '128k'];

    const out = `out.${kind}`;
    const args = ['-stream_loop', '-1', '-i', 'still.mp4', ...audioIn];
    if (kind === 'mkv') {
      await m.write('subs.srt', new TextEncoder().encode(r.srt));
      args.push('-f', 'srt', '-i', 'subs.srt', '-map', '0:v', '-map', '1:a:0', '-map', '2:s',
        '-c:v', 'copy', ...audioCodec, '-c:s', 'srt', '-disposition:s:0', 'default',
        '-metadata:s:s:0', 'title=Aligned subtitles');
    } else {
      args.push('-map', '0:v', '-map', '1:a:0', '-c:v', 'copy', ...audioCodec);
    }
    args.push('-map_chapters', '1', '-t', r.duration.toFixed(3), out);

    try {
      await m.run(args, onProgress);
      const bytes = await m.read(out);
      return new File([bytes], `${r.stem}.${this.language}.${kind}`,
        { type: kind === 'mkv' ? 'video/x-matroska' : 'video/mp4' });
    } finally {
      for (const f of [out, 'still.mp4', 'canvas.png', 'subs.srt', 'parts.txt']) await m.remove(f);
    }
  }

  /**
   * The book with the narration inside it (EPUB 3 Media Overlays): a reader
   * such as Thorium or Storyteller plays it and highlights each line.
   */
  async epub() {
    const r = this.result;
    if (!/\.epub$/i.test(this.bookFile.name)) throw new Error('A read-along book is made from an epub; this book is not one.');
    const parts = r.parts.map((p, i) => {
      const file = this.audio[i];
      // AAC counts only inside an MP4 file; a bare .aac stream is not audio an epub may hold.
      const codec = p.codec === 'aac' && !/\.(m4a|m4b|mp4)$/i.test(file.name) ? 'aac (not in an m4a file)' : p.codec;
      return { file, duration: p.duration, codec };
    });
    return syncedEpub({ book: this.bookFile, cues: r.cues, parts, stem: r.stem });
  }

  close() { this.#media?.close(); }
}

function alignInWorker(transcript, paragraphs, language) {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./align.worker.js', import.meta.url), { type: 'module' });
    worker.onmessage = ({ data }) => {
      worker.terminate();
      if (data.ok) resolve(data); else reject(new Error(data.error));
    };
    worker.onerror = (e) => { worker.terminate(); reject(new Error(e.message || 'The aligner crashed.')); };
    worker.postMessage({ transcript, paragraphs, language });
  });
}

/** The cover, letterboxed onto a 1920x1080 card; a plain dark card when there is none. */
async function canvasPng(cover) {
  const W = 1920, H = 1080;
  const canvas = new OffscreenCanvas(W, H);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#16161a';
  ctx.fillRect(0, 0, W, H);
  if (cover) {
    try {
      const blob = cover instanceof Blob ? cover : new Blob([cover.bytes]);
      const img = await createImageBitmap(blob);
      const scale = Math.min(W / img.width, H / img.height);
      const w = img.width * scale, h = img.height * scale;
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(img, (W - w) / 2, (H - h) / 2, w, h);
    } catch { /* an image the browser cannot decode: keep the plain card */ }
  }
  const png = await canvas.convertToBlob({ type: 'image/png' });
  return new Uint8Array(await png.arrayBuffer());
}

export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds)), pad = (v) => String(v).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`
    : `${Math.floor(s / 60)}:${pad(s % 60)}`;
}
