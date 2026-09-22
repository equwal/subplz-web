/* The browser's alignment engine against the reference implementation's real
 * output, stage by stage. Fixtures come from SubPlz itself, run on real
 * Whisper-tiny transcripts of a public-domain book (Aozora Bunko), with every
 * intermediate stage recorded.
 *
 *   node --test tests/engine
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import * as E from '../../frontend/engine/align.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const load = (dir) => (existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.json')).sort() : [])
  .map((f) => JSON.parse(readFileSync(path.join(dir, f), 'utf8')));
const cases = load(path.join(here, 'golden'));
assert.ok(cases.length > 0, 'no golden fixtures');

const copy = (spans) => spans.map((line) => line.map((s) => s.slice()));
const cps = (list) => list.map(E.toCodePoints);
const joined = (list) => E.toCodePoints(list.join(''));
const pathOf = (c) => ({ t: c.coords[0], q: c.coords[1] });
const texts = (cues) => cues.map((c) => c.text);

for (const c of cases) {
  const lang = E.language(c.language);

  test(`${c.name}: cleaning`, () => {
    assert.deepEqual(c.transcript.map((s) => lang.clean(s.text)), c.transcript_clean);
    assert.deepEqual(c.paragraphs.map((p) => lang.clean(p)), c.paragraphs_clean);
  });

  test(`${c.name}: alignSub`, () => {
    assert.deepEqual(E.alignSub(pathOf(c), cps(c.paragraphs_clean), cps(c.transcript_clean)), c.after_align_sub);
  });

  test(`${c.name}: fix`, () => {
    const spans = copy(c.after_align_sub);
    E.fix(lang, c.paragraphs, cps(c.paragraphs_clean), spans);
    assert.deepEqual(spans, c.after_fix);
  });

  test(`${c.name}: fixPunc`, () => {
    const spans = copy(c.after_fix);
    E.fixPunc(c.paragraphs, spans, E.PREPEND_SET, E.APPEND_SET, E.NOPEND_SET);
    assert.deepEqual(spans, c.after_fix_punc);
  });

  test(`${c.name}: toSubs`, () => {
    assert.deepEqual(E.toSubs(c.paragraphs, c.transcript, c.after_fix_punc), c.cues_raw);
  });

  test(`${c.name}: shiftAlign`, () => {
    assert.deepEqual(E.shiftAlign(c.cues_raw.map((x) => ({ ...x }))), c.cues);
  });

  test(`${c.name}: gotoh finds the optimal score`, () => {
    const target = joined(c.paragraphs_clean), query = joined(c.transcript_clean);
    const got = E.gotoh(target, query);
    assert.equal(got.score, E.pathScore(target, query, got));
    assert.equal(got.score, Math.round(c.score * 10));
  });

  test(`${c.name}: anchored alignment is as good as exact`, () => {
    const target = joined(c.paragraphs_clean), query = joined(c.transcript_clean);
    const exact = Math.round(c.score * 10);
    // A small alphabet (Latin, Cyrillic) has more near-optimal paths than kana
    // and kanji, so an anchor can fix a gap a few characters from where the
    // exact table puts it: pt_casmurro lands at 0.994 with the same cues.
    for (const [exactCells, floor] of [[4_000_000, 0.99], [40_000, 0.98], [2_500, 0.97]]) {
      const got = E.anchoredAlign(target, query, { exactCells });
      assert.equal(got.t.at(-1), target.length);
      assert.equal(got.q.at(-1), query.length);
      assert.ok(got.score / exact >= floor, `${got.score} vs ${exact} at ${exactCells}`);
    }
  });

  test(`${c.name}: whole pipeline, exact and anchored`, () => {
    assert.deepEqual(texts(E.align(c.transcript, c.paragraphs, lang)), texts(c.cues));
    const anchored = E.align(c.transcript, c.paragraphs, lang, { anchoredOnly: true, exactCells: 40_000 });
    const same = anchored.filter((cue, i) => cue.text === c.cues[i].text).length;
    assert.ok(same / c.cues.length >= 0.95, `only ${same}/${c.cues.length} cues match`);
  });
}

test('text that was never narrated is left out', () => {
  const c = cases.find((x) => x.name === 'neko_003_006');
  const other = cases.find((x) => x.name === 'neko_137_140').paragraphs;
  const lang = E.language(c.language);
  const front = other.slice(0, 6), insert = other.slice(40, 52), back = other.slice(-8);
  const mid = c.paragraphs.length >> 1;
  const clean = E.alignBook(c.transcript, c.paragraphs, lang);
  const padded = [...front, ...c.paragraphs.slice(0, mid), ...insert, ...c.paragraphs.slice(mid), ...back];
  const got = E.alignBook(c.transcript, padded, lang);
  assert.equal(got.paragraphsUsed, clean.paragraphsUsed);
  assert.deepEqual(texts(got.cues), texts(clean.cues));
  assert.ok(got.matchRate > 0.95);
});

test('srt', () => {
  assert.equal(E.stamp(59.9996), '00:01:00,000');
  assert.equal(E.stamp(12 * 3600 + 34 * 60 + 56.789), '12:34:56,789');
  assert.equal(
    E.writeSrt([{ text: 'first\nline  \r\n wrapped', start: 0, end: 1.5 }, { text: '  ', start: 1.5, end: 2 },
      { text: 'second', start: 2, end: 3.25 }]),
    '1\n00:00:00,000 --> 00:00:01,500\nfirst line wrapped\n\n2\n00:00:02,000 --> 00:00:03,250\nsecond\n\n');
});

// Local only: a whole audiobook in one pass. See subread-android/tools/make_fullbook.py.
for (const book of load(path.join(here, 'golden-local'))) {
  test('a whole book aligns in one go', () => {
    const started = performance.now();
    const got = E.alignBook(book.transcript, book.paragraphs, E.language(book.language));
    const seconds = (performance.now() - started) / 1000;
    const longest = Math.max(...got.cues.map((x) => x.text.length));
    console.log(`${got.cues.length} cues in ${seconds.toFixed(1)}s, ${(got.matchRate * 100).toFixed(1)}% matched, ` +
      `${got.paragraphsDropped} paragraphs dropped, longest cue ${longest}`);
    assert.equal(got.cues.length, book.transcript.length);
    assert.ok(got.matchRate > 0.95 && longest < 400 && seconds < 120);
  });
}
