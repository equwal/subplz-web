/* A read-along book: the epub, with the narration inside it and each line of
 * text tied to its stretch of audio (EPUB 3 Media Overlays). Thorium,
 * Storyteller and other EPUB 3 readers play it and highlight the text.
 *
 * The cues say what was read and when, but not where in the book's pages the
 * words are. That is found again by text: a cue's words, without white space,
 * are looked for in the book's words, from the end of the cue before. Each
 * find is wrapped in <span id>. A cue that crosses markup (<em>, ruby, the end
 * of a paragraph) gets several spans, and its time is divided among them by
 * their length.
 *
 * Made in the browser, like everything else: nothing is uploaded.
 */
import { zipEntries, leafBlocks, page, resolve, decode } from './book.js';
import { UNMATCHED } from './align.js';

const XHTML = 'http://www.w3.org/1999/xhtml';
const OPF = 'http://www.idpf.org/2007/opf';
const ACTIVE = '-epub-media-overlay-active';
const DIR = 'subread';                       // what this adds, beside the package file
const SHOW_TEXT = 4;

// A jump this far ahead is believed only from a cue this long: a short line
// ("Yes.") is found by chance somewhere in any book.
const FAR = 4000;
const LONG_ENOUGH = 12;

/** Audio an EPUB 3 reader must play, by codec: file extension and media type. */
const AUDIO = { mp3: ['mp3', 'audio/mpeg'], aac: ['m4a', 'audio/mp4'] };

export class NotAnEpub extends Error {}

/**
 * @param book   the epub, a File
 * @param cues   [{ text, start, end }], seconds on the clock of all the audio
 * @param parts  [{ file, duration, codec }], in playback order
 * @returns {{ file: File, located: number, of: number }}
 */
export async function syncedEpub({ book, cues, parts, stem }) {
  for (const p of parts) {
    if (!AUDIO[p.codec]) throw new Error(`A read-along book needs MP3 or AAC (m4a, m4b) audio; this is ${p.codec}.`);
  }
  const entries = zipEntries(new Uint8Array(await book.arrayBuffer()));
  const text = async (name) => decode(await entries.get(name)());
  const xml = (s) => new DOMParser().parseFromString(s, 'application/xml');

  if (!entries.has('META-INF/container.xml')) throw new NotAnEpub('This epub has no package file.');
  const opfPath = xml(await text('META-INF/container.xml')).querySelector('rootfile')?.getAttribute('full-path');
  if (!opfPath || !entries.has(opfPath)) throw new NotAnEpub('This epub has no package file.');
  const opf = xml(await text(opfPath));
  if (opf.querySelector('parsererror')) throw new NotAnEpub('The package file of this epub cannot be read.');
  const base = opfPath.includes('/') ? opfPath.slice(0, opfPath.lastIndexOf('/')) : '';
  const inBase = (p) => (base ? `${base}/${p}` : p);

  const items = new Map();                   // zip path -> manifest item
  for (const item of opf.querySelectorAll('manifest > item')) {
    items.set(resolve(base, decodeURIComponent((item.getAttribute('href') ?? '').split('#')[0])), item);
  }
  const byId = new Map([...items].map(([p, item]) => [item.getAttribute('id'), p]));
  const order = [...opf.querySelectorAll('spine > itemref')]
    .filter((r) => r.getAttribute('linear') !== 'no')
    .map((r) => byId.get(r.getAttribute('idref')))
    .filter((p) => p && entries.has(p));
  if (!order.length) throw new NotAnEpub('This epub has no reading order.');

  // The book's words, without white space, and where each one is.
  const pages = [];
  const runs = [];
  const words = [];
  let g = 0;
  for (const path of order) {
    const doc = page(await text(path));
    const pg = { path, doc, spans: [] };
    pages.push(pg);
    for (const node of textNodes(doc)) {
      const offsets = [];
      for (let i = 0; i < node.data.length; i++) if (/\S/.test(node.data[i])) offsets.push(i);
      if (!offsets.length) continue;
      runs.push({ page: pg, node, g0: g, offsets, wraps: [] });
      words.push(offsets.map((i) => node.data[i]).join(''));
      g += offsets.length;
    }
  }
  const all = words.join('');

  // Where each cue is.
  let cursor = 0, r = 0, located = 0, of = 0;
  cues.forEach((cue, index) => {
    if (cue.text.startsWith(UNMATCHED) || !(cue.end > cue.start)) return;
    const want = cue.text.replace(/\s+/g, '');
    if (!want) return;
    of++;
    const at = all.indexOf(want, cursor);
    if (at < 0 || (at - cursor > FAR && want.length < LONG_ENOUGH)) return;
    located++;
    cursor = at + want.length;

    while (runs[r].g0 + runs[r].offsets.length <= at) r++;
    const pieces = [];
    for (let k = r; k < runs.length && runs[k].g0 < cursor; k++) {
      const run = runs[k];
      const s = Math.max(at, run.g0) - run.g0, e = Math.min(cursor, run.g0 + run.offsets.length) - run.g0;
      pieces.push({ run, from: run.offsets[s], to: run.offsets[e - 1] + 1, chars: e - s });
    }
    let t = cue.start;
    pieces.forEach((piece, k) => {
      const id = `subread-${index}${k ? `-${k}` : ''}`;
      const until = k === pieces.length - 1 ? cue.end : t + (cue.end - cue.start) * piece.chars / want.length;
      piece.run.wraps.push({ from: piece.from, to: piece.to, id });
      piece.run.page.spans.push({ id, start: t, end: until });
      t = until;
    });
  });
  if (!located) throw new Error('None of the subtitles could be found in the pages of this epub.');

  for (const run of runs) if (run.wraps.length) wrap(run);

  // The new files.
  const out = new Map();                     // zip path -> Uint8Array | Blob
  const manifest = opf.querySelector('manifest');
  const metadata = opf.querySelector('metadata');
  const add = (parent, name, attributes, content) => {
    const el = opf.createElementNS(OPF, name);
    for (const [k, v] of Object.entries(attributes)) el.setAttribute(k, v);
    if (content != null) el.textContent = content;
    parent.appendChild(el);
    return el;
  };
  const encoder = new TextEncoder();

  const audio = parts.map((p, i) => {
    const [extension, type] = AUDIO[p.codec];
    const path = `${DIR}/audio-${i + 1}.${extension}`;
    out.set(inBase(path), p.file);
    add(manifest, 'item', { id: `subread-audio-${i + 1}`, href: path, 'media-type': type });
    return path;
  });
  out.set(inBase(`${DIR}/overlay.css`), encoder.encode(`.${ACTIVE} { background: #ffe08a; color: #000; }\n`));
  add(manifest, 'item', { id: 'subread-css', href: `${DIR}/overlay.css`, 'media-type': 'text/css' });

  let total = 0;
  pages.forEach((pg, n) => {
    if (!pg.spans.length) return;
    const smilPath = `${DIR}/page-${n + 1}.smil`;
    const smilDir = inBase(DIR);
    const lines = pg.spans.map((s, k) => {
      const clip = clipOf(s, parts);
      total += clip.end - clip.begin;
      return `<par id="par-${k + 1}"><text src="${attr(relative(smilDir, pg.path))}#${s.id}"/>` +
        `<audio src="${attr(relative(smilDir, inBase(audio[clip.part])))}" clipBegin="${clip.begin.toFixed(3)}s" clipEnd="${clip.end.toFixed(3)}s"/></par>`;
    });
    out.set(inBase(smilPath), encoder.encode(
      '<?xml version="1.0" encoding="utf-8"?>\n' +
      '<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">\n<body>\n' +
      `<seq id="seq" epub:textref="${attr(relative(smilDir, pg.path))}" epub:type="bodymatter">\n${lines.join('\n')}\n</seq>\n</body>\n</smil>\n`));

    const id = `subread-smil-${n + 1}`;
    add(manifest, 'item', { id, href: smilPath, 'media-type': 'application/smil+xml' });
    items.get(pg.path).setAttribute('media-overlay', id);
    const seconds = pg.spans.reduce((sum, s) => { const c = clipOf(s, parts); return sum + c.end - c.begin; }, 0);
    add(metadata, 'meta', { property: 'media:duration', refines: `#${id}` }, clock(seconds));

    const head = pg.doc.querySelector('head');
    if (head) {
      const link = pg.doc.createElementNS(XHTML, 'link');
      link.setAttribute('rel', 'stylesheet');
      link.setAttribute('type', 'text/css');
      link.setAttribute('href', relative(dirOf(pg.path), inBase(`${DIR}/overlay.css`)));
      head.appendChild(link);
    }
    out.set(pg.path, encoder.encode(serialize(pg.doc)));
  });
  add(metadata, 'meta', { property: 'media:duration' }, clock(total));
  add(metadata, 'meta', { property: 'media:active-class' }, ACTIVE);

  await toEpub3(opf, { entries, items, order, pages, base, out, add, text });
  out.set(opfPath, encoder.encode(serialize(opf)));

  // mimetype first and not compressed: that is how a reader knows an epub.
  const files = [{ name: 'mimetype', data: encoder.encode('application/epub+zip') }];
  for (const [name, read] of entries) {
    if (name !== 'mimetype' && !out.has(name)) files.push({ name, data: await read() });
  }
  for (const [name, data] of out) files.push({ name, data });
  const zip = await writeZip(files);
  return { file: new File([zip], `${stem}.read-along.epub`, { type: 'application/epub+zip' }), located, of };
}

/* --------------------------------------------------------------------- pages */

/** The text nodes that readBook() reads, in its order: leaf blocks, without furigana. */
function textNodes(doc) {
  const body = doc.querySelector('body') ?? doc.documentElement;
  const out = [];
  for (const b of leafBlocks(body)) {
    const walker = doc.createTreeWalker(b, SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.parentElement?.closest('rt, rp')) out.push(n);
    }
  }
  return out;
}

/** Replaces the text node with its pieces: plain text, and a <span id> for each find. */
function wrap({ node, wraps }) {
  const doc = node.ownerDocument, parent = node.parentNode, data = node.data;
  let at = 0;
  for (const w of wraps) {
    if (w.from > at) parent.insertBefore(doc.createTextNode(data.slice(at, w.from)), node);
    const span = doc.createElementNS(XHTML, 'span');
    span.setAttribute('id', w.id);
    span.textContent = data.slice(w.from, w.to);
    parent.insertBefore(span, node);
    at = w.to;
  }
  if (at < data.length) parent.insertBefore(doc.createTextNode(data.slice(at)), node);
  parent.removeChild(node);
}

function serialize(doc) {
  const s = new XMLSerializer().serializeToString(doc);
  return s.startsWith('<?xml') ? s : `<?xml version="1.0" encoding="utf-8"?>\n${s}`;
}

/** The stretch of one audio file that a span is read in. */
function clipOf(span, parts) {
  let base = 0, part = 0;
  while (part < parts.length - 1 && span.start >= base + parts[part].duration) base += parts[part++].duration;
  const begin = Math.max(0, span.start - base);
  // A line that runs past the end of its file stops there.
  const end = Math.max(begin + 0.001, Math.min(span.end - base, parts[part].duration || Infinity));
  return { part, begin, end };
}

export function clock(seconds) {
  const ms = Math.round(seconds * 1000);
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${Math.floor(ms / 3600000)}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)}.${p(ms % 1000, 3)}`;
}

const dirOf = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
const attr = (s) => s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

/** The way from a directory to a file, both as zip paths. */
export function relative(fromDir, to) {
  const a = fromDir ? fromDir.split('/') : [], b = to.split('/');
  while (a.length && b.length > 1 && a[0] === b[0]) { a.shift(); b.shift(); }
  return encodeURI('../'.repeat(a.length) + b.join('/'));
}

/* ----------------------------------------------------------- EPUB 2 to EPUB 3 */

/**
 * Media overlays are EPUB 3. Most epubs in the wild are EPUB 2, and an EPUB 3
 * package must have two things that those do not: a modification date and a
 * navigation document. The old table of contents (NCX) is kept too.
 */
async function toEpub3(opf, { entries, items, order, pages, base, out, add, text }) {
  const pkg = opf.documentElement;
  const metadata = opf.querySelector('metadata');
  if (!(parseFloat(pkg.getAttribute('version')) >= 3)) {
    pkg.setAttribute('version', '3.0');
    // EPUB 2 attributes that EPUB 3 does not have.
    for (const el of metadata.querySelectorAll('*')) {
      for (const a of [...el.attributes]) if (a.name.startsWith('opf:')) el.removeAttribute(a.name);
    }
    const dates = [...metadata.children].filter((el) => el.localName === 'date');
    dates.slice(1).forEach((el) => el.remove());
    // EPUB 3 wants to be told which pages hold these.
    for (const pg of pages) {
      const has = { svg: 'svg', mathml: 'math', scripted: 'script' };
      const found = Object.keys(has).filter((k) => pg.doc.getElementsByTagName(has[k]).length);
      const item = items.get(pg.path);
      const was = (item.getAttribute('properties') ?? '').split(/\s+/).filter(Boolean);
      const now = [...new Set([...was, ...found])];
      if (now.length) item.setAttribute('properties', now.join(' '));
    }
  }
  if (![...metadata.querySelectorAll('meta')].some((m) => m.getAttribute('property') === 'dcterms:modified')) {
    add(metadata, 'meta', { property: 'dcterms:modified' }, new Date().toISOString().replace(/\.\d+Z$/, 'Z'));
  }
  if ([...items.values()].some((i) => (i.getAttribute('properties') ?? '').split(/\s+/).includes('nav'))) return;

  // The contents: from the NCX when there is one, else one line for each page.
  let contents = [];
  const ncxPath = [...items].find(([, i]) => i.getAttribute('media-type') === 'application/x-dtbncx+xml')?.[0];
  if (ncxPath && entries.has(ncxPath)) {
    const ncx = new DOMParser().parseFromString(await text(ncxPath), 'application/xml');
    contents = [...ncx.querySelectorAll('navPoint')].map((p) => ({
      label: p.querySelector('navLabel > text')?.textContent.trim(),
      path: resolve(dirOf(ncxPath), decodeURIComponent(p.querySelector('content')?.getAttribute('src') ?? '')),
    })).filter((c) => c.label && c.path);
  }
  if (!contents.length) contents = order.map((path, n) => ({ label: `Part ${n + 1}`, path }));

  const navPath = `${DIR}/nav.xhtml`;
  const dir = base ? `${base}/${DIR}` : DIR;
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  out.set(`${dir}/nav.xhtml`, new TextEncoder().encode(
    '<?xml version="1.0" encoding="utf-8"?>\n' +
    `<html xmlns="${XHTML}" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>\n` +
    '<nav epub:type="toc"><h1>Contents</h1><ol>\n' +
    contents.map((c) => {
      const [file, fragment] = c.path.split('#');
      return `<li><a href="${attr(relative(dir, file))}${fragment ? `#${attr(fragment)}` : ''}">${esc(c.label)}</a></li>`;
    }).join('\n') +
    '\n</ol></nav>\n</body></html>\n'));
  add(opf.querySelector('manifest'), 'item',
    { id: 'subread-nav', href: navPath, 'media-type': 'application/xhtml+xml', properties: 'nav' });
}

/* ----------------------------------------------------------------------- zip */

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes, crc = 0) {
  let c = ~crc;
  for (let i = 0; i < bytes.length; i++) c = CRC[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return ~c >>> 0;
}

/**
 * A zip with nothing compressed: the audio, which is nearly all of it, is
 * compressed already. A Blob of parts, so a book of audio is never copied
 * into memory.
 *
 * @param files [{ name, data: Uint8Array | Blob }]
 */
export async function writeZip(files) {
  const parts = [], directory = [];
  let offset = 0;
  for (const { name, data } of files) {
    const size = data instanceof Blob ? data.size : data.length;
    let crc = 0;
    if (data instanceof Blob) {
      const reader = data.stream().getReader();
      for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) crc = crc32(chunk.value, crc);
    } else crc = crc32(data);

    const nameBytes = new TextEncoder().encode(name);
    const header = (signature, extra) => {
      const b = new DataView(new ArrayBuffer(extra ? 46 : 30));
      let o = 0;
      const u16 = (v) => { b.setUint16(o, v, true); o += 2; };
      const u32 = (v) => { b.setUint32(o, v, true); o += 4; };
      u32(signature);
      if (extra) u16(20);                    // version made by
      u16(20); u16(0x0800); u16(0);          // version needed; UTF-8 names; stored
      u16(0); u16(0x21);                     // 1980-01-01 00:00
      u32(crc); u32(size); u32(size);
      u16(nameBytes.length); u16(0);
      if (extra) { u16(0); u16(0); u16(0); u32(0); u32(offset); }
      return new Uint8Array(b.buffer);
    };
    directory.push(header(0x02014b50, true), nameBytes);
    parts.push(header(0x04034b50, false), nameBytes, data);
    offset += 30 + nameBytes.length + size;
    if (offset > 0xffffffff) throw new Error('The book and its audio are over 4 GB, which is more than an epub can hold.');
  }
  const directorySize = directory.reduce((n, p) => n + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, directorySize, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...directory, new Uint8Array(end.buffer)]);
}
