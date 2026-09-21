/* A book as a flat list of paragraphs in reading order - read in the browser,
 * never uploaded.
 *
 * epub (a zip of XHTML), fb2 (XML), Aozora Bunko (Shift_JIS text with ruby
 * markup, usually zipped) and plain text. No zip library: the platform can
 * inflate (DecompressionStream), and the little that is left of the format is
 * a directory at the end of the file.
 */

const BLOCKS = 'p, li, blockquote, h1, h2, h3, h4, h5, h6';

export async function readBook(file) {
  const name = file.name.toLowerCase();
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (name.endsWith('.epub')) return epub(bytes);
  if (name.endsWith('.fb2.zip') || name.endsWith('.fbz')) return fb2(decode(await firstEntry(bytes, /\.fb2$/i)));
  if (name.endsWith('.zip')) return aozora(decode(await firstEntry(bytes, /\.txt$/i)));
  if (name.endsWith('.fb2')) return fb2(decode(bytes));
  return plain(decode(bytes));
}

/* ----------------------------------------------------------------------- zip */

const u16 = (b, o) => b[o] | (b[o + 1] << 8);
const u32 = (b, o) => (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;

/** name -> () => Promise<Uint8Array>, in directory order. */
export function zipEntries(bytes) {
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (u32(bytes, i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('This file is not a readable zip archive.');
  const entries = new Map();
  let o = u32(bytes, eocd + 16);
  for (let n = u16(bytes, eocd + 10); n > 0 && u32(bytes, o) === 0x02014b50; n--) {
    const method = u16(bytes, o + 10), size = u32(bytes, o + 20);
    const nameLen = u16(bytes, o + 28), extraLen = u16(bytes, o + 30), commentLen = u16(bytes, o + 32);
    const local = u32(bytes, o + 42);
    const name = new TextDecoder().decode(bytes.subarray(o + 46, o + 46 + nameLen));
    if (!name.endsWith('/')) {
      entries.set(name, async () => {
        const start = local + 30 + u16(bytes, local + 26) + u16(bytes, local + 28);
        const raw = bytes.subarray(start, start + size);
        if (method === 0) return raw;
        if (method !== 8) throw new Error(`Unsupported zip compression in ${name}.`);
        const stream = new Blob([raw]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
        return new Uint8Array(await new Response(stream).arrayBuffer());
      });
    }
    o += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

async function firstEntry(bytes, pattern) {
  for (const [name, read] of zipEntries(bytes)) if (pattern.test(name)) return read();
  throw new Error('Nothing readable was found inside that archive.');
}

/** UTF-8 unless it plainly is not; then whatever the file declares, or Shift_JIS. */
export function decode(bytes) {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes).replace(/^﻿/, '');
  } catch { /* not UTF-8 */ }
  const head = new TextDecoder('latin1').decode(bytes.subarray(0, 200));
  const declared = /encoding=["']([\w-]+)["']/i.exec(head)?.[1];
  for (const label of [declared, 'shift_jis', 'windows-1251']) {
    if (!label) continue;
    try { return new TextDecoder(label).decode(bytes); } catch { /* unknown label */ }
  }
  return new TextDecoder().decode(bytes);
}

/* ---------------------------------------------------------------------- epub */

export function resolve(base, href) {
  const parts = base ? base.split('/') : [];
  for (const seg of href.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') parts.pop(); else parts.push(seg);
  }
  return parts.join('/');
}

/**
 * An epub page is XHTML, and has to be parsed as XML first: to an HTML parser
 * a self-closing <title/> never closes, and swallows the whole book as its text.
 * Plenty of epubs are not well-formed, though, so HTML is the fallback.
 */
/**
 * The blocks of text of a page, in reading order. A quote holding paragraphs
 * would yield its text twice, so only the leaves count. The sort is for DOM
 * implementations that give the matches of a selector list out of order.
 */
export function leafBlocks(body) {
  const blocks = [...body.querySelectorAll(BLOCKS)].filter((b) => !b.querySelector(BLOCKS))
    .sort((x, y) => (x.compareDocumentPosition(y) & 4 ? -1 : 1));
  return blocks.length ? blocks : [body];
}

export function page(source) {
  const xml = new DOMParser().parseFromString(source, 'application/xhtml+xml');
  if (!xml.querySelector('parsererror')) return xml;
  return new DOMParser().parseFromString(source, 'text/html');
}

async function epub(bytes) {
  const entries = zipEntries(bytes);
  const text = async (name) => decode(await entries.get(name)());
  const xml = (s) => new DOMParser().parseFromString(s, 'application/xml');
  const isPage = (n) => /\.(xhtml|html|htm)$/i.test(n);

  let order = null;
  try {
    const opfPath = xml(await text('META-INF/container.xml')).querySelector('rootfile').getAttribute('full-path');
    const opf = xml(await text(opfPath));
    const base = opfPath.includes('/') ? opfPath.slice(0, opfPath.lastIndexOf('/')) : '';
    const hrefs = new Map([...opf.querySelectorAll('manifest > item')].map((i) => [i.getAttribute('id'), i.getAttribute('href')]));
    order = [...opf.querySelectorAll('spine > itemref')]
      // linear="no" is auxiliary content outside the reading order.
      .filter((r) => r.getAttribute('linear') !== 'no')
      .map((r) => hrefs.get(r.getAttribute('idref')))
      .filter(Boolean)
      .map((h) => resolve(base, decodeURIComponent(h.split('#')[0])))
      .filter((p) => entries.has(p));
  } catch { /* malformed package file: fall through */ }
  if (!order?.length) order = [...entries.keys()].filter(isPage).sort();

  const out = [];
  let cover = null;
  for (const path of order) {
    const doc = page(await text(path));
    const body = doc.querySelector('body') ?? doc.documentElement;
    // Furigana would otherwise be read twice: once as kanji, once as kana.
    doc.querySelectorAll('rt, rp').forEach((n) => n.remove());
    for (const b of leafBlocks(body)) {
      const t = b.textContent.replace(/\s+/g, ' ').trim();
      if (t) out.push(t);
    }
  }

  // The largest image is almost always the cover, and this works on the
  // malformed epubs that converted books usually are.
  const images = [...entries.keys()].filter((n) => /\.(jpe?g|png|webp)$/i.test(n));
  if (images.length) {
    const sized = await Promise.all(images.map(async (n) => [n, (await entries.get(n)()).length]));
    const [name, size] = sized.sort((a, b) => b[1] - a[1])[0];
    if (size >= 1024) cover = { name, bytes: await entries.get(name)() };
  }
  return { paragraphs: out, cover };
}

/* ------------------------------------------------------------ other formats */

function fb2(source) {
  const doc = new DOMParser().parseFromString(source, 'application/xml');
  const bodies = [...doc.getElementsByTagName('body')]
    // Footnotes live in a second body; nobody narrates them.
    .filter((b) => (b.getAttribute('name') || '') !== 'notes');
  const out = [];
  for (const body of bodies) {
    for (const p of body.querySelectorAll('p, v, subtitle, text-author')) {
      const t = p.textContent.replace(/\s+/g, ' ').trim();
      if (t) out.push(t);
    }
  }
  return { paragraphs: out, cover: null };
}

/** Aozora Bunko: strip the legend, the colophon, ruby readings and editorial notes. */
export function aozora(raw) {
  let lines = raw.replace(/\r\n/g, '\n').split('\n');
  const rules = lines.flatMap((l, i) => (l.startsWith('-----') ? [i] : []));
  if (rules.length >= 2) lines = lines.slice(rules[1] + 1);
  const colophon = lines.findIndex((l) => l.startsWith('底本：'));
  if (colophon >= 0) lines = lines.slice(0, colophon);
  const paragraphs = lines
    .map((l) => l.replace(/［＃[^］]*］/g, '').replace(/《[^》]*》/g, '').replaceAll('｜', '').trim())
    .filter(Boolean);
  return { paragraphs, cover: null };
}

function plain(text) {
  if (text.includes('《') && text.includes('底本：')) return aozora(text);
  return { paragraphs: text.replace(/\r\n/g, '\n').split('\n').map((l) => l.trim()).filter(Boolean), cover: null };
}

/* ------------------------------------------------------------------ language */

// The commonest little words of each language. Crude, and enough: a book is
// tens of thousands of words, and the visitor can always overrule the guess.
const STOPWORDS = {
  en: 'the and of to in that was his he it with as for had you not be her on at by which have from this',
  es: 'de la que el en y a los del se las por un para con no una su al lo como más pero sus le ya',
  pt: 'de a o que e do da em um para é com não uma os no se na por mais as dos como mas foi ao ele',
  fr: 'de la le et les des en un du une que est pour qui dans a par plus pas au sur ne se ce il sont',
  de: 'der die und in den von zu das mit sich des auf für ist im dem nicht ein eine als auch es an er',
  it: 'di e il la che in a per un è del non le si con i da una dei più al come ma lo gli nel alla',
  nl: 'de van het een en in is dat op te zijn voor met die niet aan er om ook als dan maar bij hij',
  sv: 'och i att det som en på är av för med till den har de inte om ett han men var jag sig från',
  fi: 'ja on ei että oli hän se en mutta niin kun kuin joka hänen ole sen olen mitä minä jos nyt vain',
  pl: 'i w nie na z że się do to jest jak a o po ale co tak za od go był przez już tylko jego',
  tr: 'bir ve bu da de için ile ne o ben gibi çok daha ama kadar sonra en var mi diye her olan',
  ru: 'и в не на я что он с как а то это все она так его но да ты к у же вы за бы по только',
  uk: 'і в не на я що він з як а то це все вона так його але та ти до у ж ви за би по тільки',
};

/** Best guess at a Whisper language code for `paragraphs`, or null. */
export function detectLanguage(paragraphs) {
  const sample = paragraphs.join(' ').slice(0, 60000);
  const count = (re) => (sample.match(re) || []).length;
  const kana = count(/[぀-ヿ]/g), han = count(/[一-鿿]/g), hangul = count(/[가-힯]/g);
  if (kana > 50) return 'ja';
  if (hangul > 50) return 'ko';
  if (han > 200) return 'zh';
  if (count(/[Ͱ-Ͽ]/g) > 200) return 'el';
  if (count(/[֐-׿]/g) > 200) return 'he';
  if (count(/[؀-ۿ]/g) > 200) return 'ar';
  if (count(/[฀-๿]/g) > 200) return 'th';

  const words = sample.toLowerCase().match(/\p{L}+/gu) || [];
  if (words.length < 50) return null;
  const tally = new Map();
  for (const w of words) tally.set(w, (tally.get(w) || 0) + 1);
  let best = null, bestScore = 0;
  for (const [code, list] of Object.entries(STOPWORDS)) {
    const score = list.split(' ').reduce((n, w) => n + (tally.get(w) || 0), 0);
    if (score > bestScore) { best = code; bestScore = score; }
  }
  // Fewer than one word in twelve a stopword of the winner: not one of ours.
  return bestScore / words.length > 0.08 ? best : null;
}
