/* Kindle books are read in the browser, by foliate-js. The same public-domain
 * book (The Yellow Wallpaper, Project Gutenberg #1952) as a MOBI/KF8 file and
 * as an epub must give the same text.
 *
 *   node --test tests/engine
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

// foliate-js unescapes HTML entities through a <textarea>, so it wants a document too.
const { window } = new JSDOM('');
Object.assign(globalThis, { DOMParser: window.DOMParser, XMLSerializer: window.XMLSerializer, document: window.document });

const { readBook } = await import('../../frontend/engine/book.js');

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = (name) => new File([readFileSync(path.join(here, 'fixtures', name))], name);
const bare = (s) => s.replace(/\s+/g, '').toLowerCase();

test('a Kindle file gives the paragraphs of the book, as the epub does', async () => {
  const mobi = await readBook(fixture('yellow-wallpaper.mobi'));
  const epub = await readBook(fixture('yellow-wallpaper.epub'));
  assert.ok(epub.paragraphs.length > 100, `epub has ${epub.paragraphs.length} paragraphs`);
  assert.ok(mobi.paragraphs.length > 100, `mobi has ${mobi.paragraphs.length} paragraphs`);

  // The story itself, not the licence, which each edition words its own way.
  const story = (p) => p.filter((t) => t.length > 40);
  const inMobi = new Set(story(mobi.paragraphs).map(bare));
  const shared = story(epub.paragraphs).filter((t) => inMobi.has(bare(t))).length;
  const share = shared / story(epub.paragraphs).length;
  assert.ok(share > 0.9, `${(share * 100).toFixed(0)}% of the epub's paragraphs are in the mobi`);

  // In reading order: the first line of the story before its last.
  const first = mobi.paragraphs.findIndex((t) => /very seldom that mere ordinary people/.test(t));
  const last = mobi.paragraphs.findIndex((t) => /had to creep over him every time/.test(t));
  assert.ok(first >= 0 && last > first, `first line at ${first}, last at ${last}`);
});

test('a Kindle file that is not one is refused, not read as text', async () => {
  await assert.rejects(readBook(new File([new Uint8Array(100)], 'broken.azw3')));
});
