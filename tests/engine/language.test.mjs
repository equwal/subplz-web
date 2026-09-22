/* The languages SubRead is checked against: English, Portuguese, Spanish,
 * Russian and Japanese. The text of each comes from the golden fixtures, so
 * the same public-domain excerpts serve the guess of the language and the
 * alignment.
 *
 *   node --test tests/engine
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { LANGUAGES } from '../../frontend/engine/languages.js';
import { detectLanguage } from '../../frontend/engine/book.js';
import { language, align, UNMATCHED } from '../../frontend/engine/align.js';

export const CHECKED = ['en', 'pt', 'es', 'ru', 'ja'];

const here = path.dirname(fileURLToPath(import.meta.url));
const fixtures = readdirSync(path.join(here, 'golden')).filter((f) => f.endsWith('.json')).sort()
  .map((f) => JSON.parse(readFileSync(path.join(here, 'golden', f), 'utf8')));

/** The first fixture of each language. */
export const byLanguage = new Map(CHECKED.map((code) => [code, fixtures.find((c) => c.language === code)]));

test('each checked language has a golden fixture', () => {
  for (const code of CHECKED) assert.ok(byLanguage.get(code), `no golden fixture for ${code}`);
});

test('each checked language is offered to the visitor', () => {
  const offered = new Set(LANGUAGES.map((l) => l.code));
  for (const code of CHECKED) assert.ok(offered.has(code), `${code} is not in LANGUAGES`);
});

test('the language of a book is guessed from its text', () => {
  for (const [code, c] of byLanguage) assert.equal(detectLanguage(c.paragraphs), code, `guess for ${code}`);
});

test('a page of a book is enough to guess the language', () => {
  for (const [code, c] of byLanguage) {
    const page = c.paragraphs.slice(0, 3);
    assert.equal(detectLanguage(page), code, `guess for ${code} from ${page.join('').length} characters`);
  }
});

test('cleaning folds case in every script and keeps the letters', () => {
  assert.equal(language('ru').clean('Ёлка И Её'), 'ёлка и её');
  assert.equal(language('pt').clean('Não É Coração'), 'não é coração');
  assert.equal(language('es').clean('¿Qué? ¡Ñandú!'), '¿qué? ¡ñandú!');
  assert.equal(language('en').clean('The Yellow Wallpaper'), 'the yellow wallpaper');
  // Only Japanese drops punctuation and spacing, and folds the particles the recogniser confuses.
  assert.equal(language('ja').clean('吾輩は　猫である。'), '吾輩わ猫でわる。');
});

test('a transcript with no words in common with the book gives unmatched cues, not a crash', () => {
  for (const [code, c] of byLanguage) {
    const other = byLanguage.get(code === 'ja' ? 'en' : 'ja');
    const cues = align(other.transcript.slice(0, 20), c.paragraphs.slice(0, 5), language(code));
    assert.equal(cues.length, 20, code);
    for (const cue of cues) assert.ok(typeof cue.text === 'string' && cue.text.length > 0, code);
  }
});

test('the whole pipeline keeps the words of the book, in order, for each language', () => {
  for (const [code, c] of byLanguage) {
    const cues = align(c.transcript, c.paragraphs, language(code));
    const matched = cues.filter((cue) => !cue.text.startsWith(UNMATCHED));
    assert.ok(matched.length / cues.length >= 0.95, `${code}: ${matched.length}/${cues.length} matched`);
    // Every matched cue is a slice of the book, and the slices come in reading order.
    const squeeze = (s) => s.replace(/\s+/g, '');
    const book = squeeze(c.paragraphs.join(''));
    let at = 0;
    for (const cue of matched) {
      const found = book.indexOf(squeeze(cue.text), at);
      assert.ok(found >= 0, `${code}: cue not in the book after position ${at}: ${cue.text}`);
      at = found;
    }
    for (let i = 1; i < cues.length; i++) assert.ok(cues[i].start >= cues[i - 1].start, `${code}: cue ${i} starts before cue ${i - 1}`);
  }
});
