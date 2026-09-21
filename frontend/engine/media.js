/* Reading and writing media in the browser, with ffmpeg compiled to WebAssembly.
 *
 * The visitor's file is never copied anywhere: it is mounted read-only into
 * ffmpeg's filesystem (WORKERFS), which reads straight from the File on disk.
 * That is what makes a 600 MB audiobook workable - the only audio ever held in
 * memory is the couple of minutes being transcribed.
 *
 * ffmpeg runs in its own worker; everything here is async message-passing.
 */
import { FFmpeg } from '/vendor/ffmpeg/index.js';

const CORE = new URL('/vendor/ffmpeg/ffmpeg-core.js', import.meta.url).href;
const RATE = 16000;

export class Media {
  #ffmpeg = new FFmpeg();
  #log = [];
  #mounted = [];

  /** Bytes fetched so far while the 32 MB core downloads, for a progress bar. */
  static async open(onLog) {
    const m = new Media();
    m.#ffmpeg.on('log', ({ message }) => {
      m.#log.push(message);
      if (m.#log.length > 400) m.#log.shift();
      onLog?.(message);
    });
    await m.#ffmpeg.load({ coreURL: CORE });
    return m;
  }

  /** Make `file` readable by ffmpeg at the returned path, without copying it. */
  async mount(file, dir) {
    await this.#ffmpeg.createDir(dir).catch(() => {});
    await this.#ffmpeg.mount('WORKERFS', { files: [file] }, dir);
    this.#mounted.push(dir);
    return `${dir}/${file.name}`;
  }

  /** Duration in seconds and chapter marks, read from ffmpeg's own banner. */
  async probe(path) {
    this.#log.length = 0;
    await this.#ffmpeg.exec(['-hide_banner', '-i', path]);   // "fails": no output given. Expected.
    const text = this.#log.join('\n');
    const d = /Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)/.exec(text);
    if (!d) throw new Error('This file does not look like audio ffmpeg can read.');
    const audio = /Stream #\d+:\d+.*?: Audio: ([a-z0-9_]+)/i.exec(text);
    return {
      duration: (+d[1]) * 3600 + (+d[2]) * 60 + (+d[3]),
      codec: audio ? audio[1].toLowerCase() : null,
      chapters: [...text.matchAll(/Chapter #\d+:\d+: start ([\d.]+), end ([\d.]+)/g)]
        .map((c) => ({ start: +c[1], end: +c[2] })),
    };
  }

  /** `seconds` of audio from `start`, as the 16 kHz mono float PCM the speech model takes. */
  async pcm(path, start, seconds) {
    const out = 'chunk.f32';
    const code = await this.#ffmpeg.exec([
      '-hide_banner', '-v', 'error',
      // Tolerate damaged files: drop what cannot be decoded, keep going.
      '-err_detect', 'ignore_err', '-fflags', '+discardcorrupt',
      '-ss', String(start), '-t', String(seconds), '-i', path,
      '-map', '0:a:0', '-vn', '-sn', '-ac', '1', '-ar', String(RATE), '-f', 'f32le', out,
    ]);
    if (code !== 0) throw new Error(`Could not decode the audio at ${Math.round(start)}s.`);
    const bytes = await this.#ffmpeg.readFile(out);
    await this.#ffmpeg.deleteFile(out);
    return new Float32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength >> 2);
  }

  async write(name, data) { await this.#ffmpeg.writeFile(name, data); }
  async read(name) { return this.#ffmpeg.readFile(name); }
  async remove(name) { await this.#ffmpeg.deleteFile(name).catch(() => {}); }

  /** Run ffmpeg; throws with the tail of its log when it fails. */
  async run(args, onProgress) {
    this.#log.length = 0;
    const handler = onProgress && (({ progress }) => onProgress(Math.min(Math.max(progress, 0), 1)));
    if (handler) this.#ffmpeg.on('progress', handler);
    try {
      const code = await this.#ffmpeg.exec(['-hide_banner', '-y', ...args]);
      if (code !== 0) throw new Error(this.#log.slice(-3).join(' | ') || `ffmpeg exited with ${code}`);
    } finally {
      if (handler) this.#ffmpeg.off('progress', handler);
    }
  }

  close() { this.#ffmpeg.terminate(); }
}

/**
 * Where to cut a chunk: the quietest quarter second in its last few seconds,
 * so that no word is split across two transcriptions.
 * Returns a sample index into `pcm`.
 */
export function quietestPoint(pcm, searchSeconds = 12, windowSeconds = 0.25) {
  const window = Math.floor(windowSeconds * RATE);
  const from = Math.max(0, pcm.length - searchSeconds * RATE);
  if (pcm.length - from <= window * 2) return pcm.length;
  let energy = 0;
  for (let i = from; i < from + window; i++) energy += Math.abs(pcm[i]);
  let best = energy, bestAt = from;
  for (let i = from; i + window < pcm.length; i++) {
    energy += Math.abs(pcm[i + window]) - Math.abs(pcm[i]);
    if (energy < best) { best = energy; bestAt = i + 1; }
  }
  return bestAt + (window >> 1);
}

export const SAMPLE_RATE = RATE;
