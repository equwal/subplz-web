/* Alignment off the main thread: a long book is a few seconds of solid work. */
import { alignBook, language, writeSrt } from './align.js';

self.onmessage = ({ data }) => {
  try {
    const result = alignBook(data.transcript, data.paragraphs, language(data.language));
    self.postMessage({ ok: true, ...result, srt: writeSrt(result.cues) });
  } catch (e) {
    self.postMessage({ ok: false, error: String(e?.message || e) });
  }
};
