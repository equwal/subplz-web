/* The read-along epub: made from a small EPUB 2 book with the markup that makes
 * this hard (inline elements, furigana, pages in folders, a space in a file
 * name), then read back and checked the way a reader would use it.
 *
 *   node --test tests/engine
 *
 * With EPUBCHECK=<path to epubcheck.jar> the W3C validator judges the result too.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const { window } = new JSDOM('');
Object.assign(globalThis, { DOMParser: window.DOMParser, XMLSerializer: window.XMLSerializer });

const { syncedEpub, writeZip, relative, clock } = await import('../../frontend/engine/epub.js');
const { zipEntries, readBook, decode } = await import('../../frontend/engine/book.js');
const E = await import('../../frontend/engine/align.js');

const enc = new TextEncoder();
const bare = (s) => s.replace(/\s+/g, '');

const page = (body) => `<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>A Test</title></head><body>${body}</body></html>`;

async function book() {
  const files = {
    mimetype: 'application/epub+zip',
    'META-INF/container.xml': `<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>`,
    'OEBPS/content.opf': `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>A Test</dc:title><dc:language>en</dc:language><dc:identifier id="id">urn:uuid:0e6a7c8e-1111-4222-8333-444455556666</dc:identifier>
        <dc:creator opf:role="aut">Nobody</dc:creator><dc:date opf:event="publication">2001-01-01</dc:date><dc:date opf:event="modification">2002-02-02</dc:date>
      </metadata>
      <manifest>
        <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
        <item id="front" href="front.xhtml" media-type="application/xhtml+xml"/>
        <item id="one" href="text/chapter%20one.xhtml" media-type="application/xhtml+xml"/>
        <item id="two" href="text/two.xhtml" media-type="application/xhtml+xml"/>
      </manifest>
      <spine toc="ncx"><itemref idref="front"/><itemref idref="one"/><itemref idref="two"/></spine></package>`,
    'OEBPS/toc.ncx': `<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>
      <meta name="dtb:uid" content="urn:uuid:0e6a7c8e-1111-4222-8333-444455556666"/></head><docTitle><text>A Test</text></docTitle><navMap>
      <navPoint id="n1" playOrder="1"><navLabel><text>One &amp; only</text></navLabel><content src="text/chapter%20one.xhtml"/></navPoint>
      <navPoint id="n2" playOrder="2"><navLabel><text>Two</text></navLabel><content src="text/two.xhtml#top"/></navPoint></navMap></ncx>`,
    'OEBPS/front.xhtml': page('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>Cover</title><rect width="10" height="10"/></svg><h1>A Test</h1><p>Printed somewhere, by someone. Nobody reads this aloud.</p>'),
    'OEBPS/text/chapter one.xhtml': page('<h2>Chapter one</h2><p>It was a <em>dark and stormy</em> night; the rain fell in torrents.</p>' +
      '<p>Yes.</p><p>She said &lt;no&gt; &amp; left.</p>'),
    'OEBPS/text/two.xhtml': page('<h2 id="top">Two</h2><p><ruby>吾輩<rt>わがはい</rt></ruby>は<ruby>猫<rt>ねこ</rt></ruby>である。名前はまだ無い。</p><p>Yes.</p>'),
  };
  const zip = await writeZip(Object.entries(files).map(([name, s]) => ({ name, data: enc.encode(s) })));
  return new File([zip], 'test.epub');
}

const cues = [
  { text: 'Chapter one It was a dark', start: 1, end: 3 },            // a heading, a paragraph, and into <em>
  { text: 'and stormy night; the rain fell in torrents.', start: 3, end: 6 },
  { text: E.UNMATCHED + 'mumble', start: 6, end: 7 },                 // not from the book: no place in it
  { text: 'Yes.', start: 7, end: 8 },
  { text: 'She said <no> & left.', start: 8, end: 10 },
  { text: 'Two 吾輩は猫である。', start: 61, end: 64 },                   // in the second audio file
  { text: '名前はまだ無い。', start: 64, end: 66 },
  { text: 'Yes.', start: 66, end: 67 },
];
const audio = (n) => new File([new Uint8Array(2000).fill(n)], `part${n}.mp3`);
const parts = [{ file: audio(1), duration: 60, codec: 'mp3' }, { file: audio(2), duration: 30, codec: 'mp3' }];

test('each cue is tied to its own words in the pages, and to its stretch of audio', async () => {
  const { file, located, of } = await syncedEpub({ book: await book(), cues, parts, stem: 'test' });
  assert.equal(of, 7);
  assert.equal(located, 7);
  assert.equal(file.name, 'test.read-along.epub');

  const bytes = new Uint8Array(await file.arrayBuffer());
  // A reader knows an epub by these bytes at these places.
  assert.equal(decode(bytes.subarray(30, 38)), 'mimetype');
  assert.equal(decode(bytes.subarray(38, 58)), 'application/epub+zip');

  const entries = zipEntries(bytes);
  const read = async (name) => decode(await entries.get(name)());
  const xml = (s) => new window.DOMParser().parseFromString(s, 'application/xml');

  const opf = xml(await read('OEBPS/content.opf'));
  assert.equal(opf.documentElement.getAttribute('version'), '3.0');
  const items = [...opf.querySelectorAll('manifest > item')];
  const item = (id) => items.find((i) => i.getAttribute('id') === id);
  assert.equal(item('front').getAttribute('media-overlay'), null, 'nothing of the front page is read');
  assert.equal(item('front').getAttribute('properties'), 'svg');
  assert.equal(item('one').getAttribute('properties'), null);

  // Follow each overlay as a reader does: SMIL -> the element in the page, and the clip.
  const said = new Map();                    // cue index -> the words its spans hold
  const clips = [];
  for (const id of ['one', 'two']) {
    const smilItem = item(item(id).getAttribute('media-overlay'));
    assert.equal(smilItem.getAttribute('media-type'), 'application/smil+xml');
    const smilPath = `OEBPS/${smilItem.getAttribute('href')}`;
    const smil = xml(await read(smilPath));
    assert.equal(smil.querySelector('parsererror'), null);
    const from = path.posix.dirname(smilPath);
    for (const par of smil.querySelectorAll('par')) {
      const [src, fragment] = par.querySelector('text').getAttribute('src').split('#');
      const pagePath = path.posix.normalize(`${from}/${decodeURI(src)}`);
      assert.equal(pagePath, `OEBPS/${decodeURIComponent(item(id).getAttribute('href'))}`);
      const doc = xml(await read(pagePath));
      assert.equal(doc.querySelector('parsererror'), null, `${pagePath} is not well-formed`);
      const el = doc.getElementById(fragment);
      assert.ok(el, `${fragment} is not in ${pagePath}`);
      const index = Number(fragment.split('-')[1]);
      said.set(index, (said.get(index) ?? '') + el.textContent);

      const a = par.querySelector('audio');
      const audioPath = path.posix.normalize(`${from}/${decodeURI(a.getAttribute('src'))}`);
      assert.ok(entries.has(audioPath), audioPath);
      clips.push({ index, audioPath, begin: parseFloat(a.getAttribute('clipBegin')), end: parseFloat(a.getAttribute('clipEnd')) });
    }
  }
  cues.forEach((cue, i) => {
    if (cue.text.startsWith(E.UNMATCHED)) assert.ok(!said.has(i));
    else assert.equal(bare(said.get(i)), bare(cue.text), `cue ${i}`);
  });

  // The pieces of a cue share its time, in order, inside the right file.
  for (const [i, cue] of cues.entries()) {
    const mine = clips.filter((c) => c.index === i);
    if (!mine.length) continue;
    const base = cue.start >= 60 ? 60 : 0;
    assert.ok(mine.every((c) => c.audioPath === `OEBPS/subread/audio-${base ? 2 : 1}.mp3`));
    assert.ok(Math.abs(mine[0].begin - (cue.start - base)) < 0.002);
    assert.ok(Math.abs(mine.at(-1).end - (cue.end - base)) < 0.002);
    mine.forEach((c, k) => { assert.ok(c.end > c.begin); if (k) assert.ok(Math.abs(c.begin - mine[k - 1].end) < 0.002); });
  }
  assert.equal(clips.filter((c) => c.index === 0).length, 3, 'heading, paragraph text, and the text inside <em>');

  // The second "Yes." is the one in chapter two, not the first again.
  const two = xml(await read('OEBPS/text/two.xhtml'));
  assert.equal(two.getElementById('subread-7').textContent, 'Yes.');
  // Furigana is still in the book, and outside the spans.
  assert.equal(two.querySelectorAll('rt').length, 2);
  assert.equal([...two.querySelectorAll('span[id^=subread]')].some((s) => s.querySelector('rt') || s.closest('rt')), false);
  // The words of the book are as they were.
  const before = await readBook(await book());
  const after = await readBook(file);
  assert.deepEqual(after.paragraphs, before.paragraphs);

  // The audio is in the book, byte for byte.
  assert.deepEqual(await entries.get('OEBPS/subread/audio-2.mp3')(), new Uint8Array(2000).fill(2));

  // What EPUB 3 asks for that EPUB 2 did not have.
  const metas = [...opf.querySelectorAll('metadata > meta')];
  const meta = (property) => metas.filter((m) => m.getAttribute('property') === property);
  assert.equal(meta('dcterms:modified').length, 1);
  assert.equal(meta('media:active-class')[0].textContent, '-epub-media-overlay-active');
  assert.equal(meta('media:duration').length, 3);              // two overlays and the total
  assert.equal(meta('media:duration').find((m) => !m.getAttribute('refines')).textContent, clock(2 + 3 + 1 + 2 + 3 + 2 + 1));
  const nav = xml(await read(`OEBPS/${items.find((i) => i.getAttribute('properties') === 'nav').getAttribute('href')}`));
  assert.deepEqual([...nav.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]),
    [['One & only', '../text/chapter%20one.xhtml'], ['Two', '../text/two.xhtml#top']]);

  const jar = process.env.EPUBCHECK;
  if (jar && existsSync(jar)) {
    const dir = mkdtempSync(path.join(tmpdir(), 'subread-'));
    try {
      const out = path.join(dir, 'test.epub');
      writeFileSync(out, bytes);
      const r = spawnSync('java', ['-jar', jar, out], { encoding: 'utf8' });
      assert.equal(r.status, 0, r.stdout + r.stderr);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  }
});

test('audio that an EPUB 3 reader need not play is refused', async () => {
  await assert.rejects(
    syncedEpub({ book: await book(), cues, parts: [{ file: audio(1), duration: 90, codec: 'flac' }], stem: 'x' }),
    /MP3 or AAC/);
});

test('subtitles of another book are refused', async () => {
  await assert.rejects(
    syncedEpub({ book: await book(), cues: [{ text: 'Call me Ishmael. Some years ago', start: 0, end: 2 }], parts, stem: 'x' }),
    /None of the subtitles/);
});

test('a short line is not looked for far ahead', async () => {
  // "Yes." is next only in chapter two... but here chapter one's is near, so it is found there.
  const r = await syncedEpub({ book: await book(), cues: [{ text: 'Yes.', start: 0, end: 1 }], parts, stem: 'x' });
  assert.equal(r.located, 1);
});

test('a zip that is written reads back the same, whatever is in it', async () => {
  let seed = 7;
  const random = () => (seed = (seed * 1103515245 + 12345) >>> 0) / 2 ** 32;
  for (let round = 0; round < 25; round++) {
    const files = Array.from({ length: 1 + Math.floor(random() * 6) }, (_, i) => {
      const data = Uint8Array.from({ length: Math.floor(random() * 3000) }, () => Math.floor(random() * 256));
      return { name: `dir ${i}/ファイル-${round}-${i}.bin`, data: i % 2 ? new Blob([data]) : data, bytes: data };
    });
    const back = zipEntries(new Uint8Array(await (await writeZip(files)).arrayBuffer()));
    assert.deepEqual([...back.keys()], files.map((f) => f.name));
    for (const f of files) assert.deepEqual(await back.get(f.name)(), f.bytes);
  }
  // And a standard tool agrees about the checksums.
  const dir = mkdtempSync(path.join(tmpdir(), 'subread-'));
  try {
    const out = path.join(dir, 'a.zip');
    writeFileSync(out, new Uint8Array(await (await writeZip([{ name: 'a.txt', data: enc.encode('hello') }])).arrayBuffer()));
    const r = spawnSync('python', ['-c', 'import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None; print(z.read("a.txt").decode())', out], { encoding: 'utf8' });
    if (!r.error) assert.equal(r.stdout.trim(), 'hello', r.stderr);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('relative paths', () => {
  assert.equal(relative('OEBPS/subread', 'OEBPS/text/a b.xhtml'), '../text/a%20b.xhtml');
  assert.equal(relative('OEBPS/text', 'OEBPS/text/two.xhtml'), 'two.xhtml');
  assert.equal(relative('', 'subread/overlay.css'), 'subread/overlay.css');
  assert.equal(relative('a/b', 'c.css'), '../../c.css');
});

/* A real book, when it is on this machine (it is in copyright, so it is not in the repository). */
const local = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/(\w:)/, '$1')), 'excerpt-local');
const realBook = path.join(local, 'moskva.epub'), realAudio = path.join(local, 'ru.mp3');
test('a real book: nearly each line is found, and the validator accepts the result',
  { skip: !(existsSync(realBook) && existsSync(realAudio)) && 'no local book' }, async () => {
    const fixture = JSON.parse(readFileSync(path.join(local, 'moskva_device.json'), 'utf8'));
    const bookFile = new File([readFileSync(realBook)], 'moskva.epub');
    const { paragraphs } = await readBook(bookFile);
    const { cues } = E.alignBook(fixture.transcript, paragraphs, E.language(fixture.language));
    const duration = fixture.transcript.at(-1).end + 1;
    const r = await syncedEpub({
      book: bookFile, cues, stem: 'moskva',
      parts: [{ file: new File([readFileSync(realAudio)], 'ru.mp3'), duration, codec: 'mp3' }],
    });
    console.log(`located ${r.located} of ${r.of} cues; ${r.file.size} bytes`);
    assert.ok(r.located >= 0.95 * r.of, `${r.located} of ${r.of}`);
    assert.deepEqual((await readBook(r.file)).paragraphs, paragraphs);

    const out = process.env.SUBREAD_KEEP_EPUB;
    if (out) writeFileSync(out, new Uint8Array(await r.file.arrayBuffer()));
    const jar = process.env.EPUBCHECK;
    if (jar && existsSync(jar) && out) {
      const before = spawnSync('java', ['-jar', jar, realBook], { encoding: 'utf8' });
      const after = spawnSync('java', ['-jar', jar, out], { encoding: 'utf8' });
      const errors = (s) => Number(/(\d+) errors?/.exec(s.stdout + s.stderr)?.[1] ?? -1);
      console.log(`epubcheck errors: ${errors(before)} in the source, ${errors(after)} in the result`);
      console.log((after.stdout + after.stderr).split('\n').filter((l) => /ERROR|FATAL/.test(l)).slice(0, 12).join('\n'));
    }
  });
