// Helpers and note flows of the Obsidian templates (#344). `zettel/obsidian.py`
// prepends the vault timezone and appends RENDER (the note bodies, generated from
// zettel/vault.py), and writes the result to 99_Templates/scripts/zettel.js.
// Each helper mirrors a Python function; tests/test_obsidian_templates.py runs
// both on the same inputs.

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

// zettel.vault.note_filename / ULID: 48-bit time + 80 random bits, Crockford base32.
function ulid(now = Date.now()) {
  let time = "";
  let t = now;
  for (let i = 0; i < 10; i++) {
    time = CROCKFORD[t % 32] + time;
    t = Math.floor(t / 32);
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  let random = "";
  for (const b of bytes) random += CROCKFORD[b % 32];
  return time + random;
}

// zettel.vault._slug
function slug(text, max = 100) {
  let s = String(text ?? "").toLowerCase().trim();
  s = s.replace(/[^\p{L}\p{N}_\s-]/gu, "");
  s = s.replace(/[\s_]+/gu, "-");
  return Array.from(s).slice(0, max).join("").replace(/-+$/u, "");
}

// zettel.vault.safe_vault_component
function safeComponent(value) {
  const text = String(value ?? "").replace(/\\/g, "/");
  const kept = text.split("/").filter((part) => !["", ".", ".."].includes(part));
  const cleaned = kept.join("-").replace(/\.\./g, "").trim().replace(/^\.+|\.+$/g, "");
  return cleaned || "nota";
}

// zettel.vault.author_year_label
function authorYearLabel(citekey) {
  const key = safeComponent(String(citekey ?? "").replace(/^@+/, ""));
  const match = key.match(/^(\p{L}+\p{Nd}{4})/u);
  return match ? match[1] : key;
}

function pad(n, width) {
  return String(Math.trunc(Number(n))).padStart(width, "0");
}

// zettel.vault.note_filename, without ".md"
function noteStem(prefix, identifier, title) {
  return `${prefix} - ${safeComponent(identifier)} - ${slug(title) || "nota"}`;
}

function sourceNoteStem(citekey, title) {
  return noteStem("SRC", authorYearLabel(citekey), title);
}

// zettel.vault.literature_index_filename, without ".md"
function literatureIndexStem(citekey, title) {
  return `LIT - ${authorYearLabel(citekey)} - ${title ? slug(title) : "index"}`;
}

// zettel.vault.literature_chunk_filename for a manual note (topic = section_path)
function literatureChunkStem(citekey, chunkIndex, page, topic) {
  const token = page !== null && page !== undefined ? `p${pad(page, 3)}` : `c${pad(chunkIndex, 4)}`;
  const last = String(topic ?? "").split(">").pop().trim();
  const section = ["documento completo", ""].includes(last.toLowerCase()) ? "" : last;
  const topicSlug = (section && slug(section, 40)) || "nota";
  return `LIT - ${authorYearLabel(citekey)} - ${token} - ${topicSlug}-${pad(chunkIndex, 4)}`;
}

// zettel.harvester.citekey.citekey_word
function citekeyWord(text) {
  return Array.from(String(text ?? "").normalize("NFC"))
    .filter((ch) => /[\p{L}\p{N}]/u.test(ch))
    .join("");
}

function capitalize(word) {
  return word ? word[0].toUpperCase() + word.slice(1).toLowerCase() : word;
}

// zettel.new_note.provisional_citekey
function provisionalCitekey(authors, year, title) {
  const first = (authors && authors[0]) || "";
  const parts = first.trim().split(/\s+/).filter(Boolean);
  const surname = parts.length ? citekeyWord(parts[parts.length - 1]) : "";
  const words = String(title ?? "")
    .replace(/[^\p{L}\p{N}_\s]/gu, "")
    .split(/\s+/)
    .map(citekeyWord)
    .filter(Boolean);
  const head = (n) => words.slice(0, n).map(capitalize).join("") || "Untitled";
  const hasYear = year !== null && year !== undefined;
  if (surname && hasYear) return `${surname}${year}${head(2)}`;
  if (surname) return `${surname}${head(3)}`;
  if (hasYear) return `${year}${head(3)}`;
  return words.slice(0, 4).map(capitalize).join("") || "Untitled";
}

// zettel.harvester.citekey._disambiguation_suffix
function disambiguation(index) {
  let n = index;
  let out = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    n = Math.floor((n - 1) / 26);
    out = String.fromCharCode(97 + rem) + out;
  }
  return out;
}

// zettel.time.now_vault_iso: ISO 8601 to the second, with the vault's offset.
function vaultNow(date = new Date()) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: VAULT_TIMEZONE,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(date)
      .map((p) => [p.type, p.value]),
  );
  const local = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  const offset = Math.round((local - Math.floor(date.getTime() / 1000) * 1000) / 60000);
  const sign = offset < 0 ? "-" : "+";
  const abs = Math.abs(offset);
  return (
    `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}` +
    `${sign}${pad(Math.floor(abs / 60), 2)}:${pad(abs % 60, 2)}`
  );
}

// Frontmatter as `key: <JSON>` lines: JSON is valid YAML, so a title with ":" or
// "#" stays one string. An undefined value drops the key; null is written.
function frontmatter(pairs) {
  const lines = pairs.filter(([, value]) => value !== undefined).map(([k, v]) => `${k}: ${JSON.stringify(v)}`);
  return `---\n${lines.join("\n")}\n---\n`;
}

// -- vault access (Obsidian's global `app`) ---------------------------------

function markdownFiles(folder) {
  return app.vault.getMarkdownFiles().filter((f) => f.path.startsWith(`${folder}/`));
}

function frontmatterOf(file) {
  return (app.metadataCache.getFileCache(file) || {}).frontmatter || {};
}

function listSources() {
  return markdownFiles("10_Sources")
    .map((file) => {
      const fm = frontmatterOf(file);
      const sourceId = String(fm.source_id || "");
      if (!sourceId.startsWith("@")) return null;
      const citekey = String(fm.citekey || sourceId.slice(1));
      return { source_id: sourceId, citekey, title: String(fm.title || file.basename), stem: file.basename };
    })
    .filter(Boolean)
    .sort((a, b) => a.title.localeCompare(b.title));
}

function nextChunkIndex(citekey) {
  let max = 0;
  for (const file of markdownFiles(`20_Literature/${safeComponent(citekey)}`)) {
    const n = Number(frontmatterOf(file).chunk_index);
    if (Number.isInteger(n) && n > max) max = n;
  }
  return max + 1;
}

function uniqueCitekey(base) {
  const taken = new Set(listSources().map((s) => s.citekey));
  let key = base;
  for (let i = 1; taken.has(key); i++) key = `${base}${disambiguation(i)}`;
  return key;
}

async function ensureFolder(path) {
  if (!app.vault.getAbstractFileByPath(path)) await app.vault.createFolder(path);
}

async function ask(tp, label, required = true) {
  const answer = String((await tp.system.prompt(label)) ?? "").trim();
  if (required && !answer) throw new Error(`Zettel: "${label}" e obrigatorio.`);
  return answer;
}

async function askInt(tp, label) {
  const answer = await ask(tp, label, false);
  if (!answer) return null;
  const n = Number(answer);
  if (!Number.isInteger(n)) throw new Error(`Zettel: "${label}" deve ser um numero inteiro.`);
  return n;
}

async function pickSource(tp, optional) {
  const sources = listSources();
  if (!sources.length && !optional) {
    throw new Error("Zettel: nenhuma SRC em 10_Sources/. Crie a fonte primeiro (template SRC).");
  }
  const items = optional ? [null, ...sources] : sources;
  const labels = items.map((s) => (s ? `${s.title} (${s.source_id})` : "(sem fonte)"));
  const choice = await tp.system.suggester(labels, items, false, "Fonte da nota");
  if (!choice && !optional) throw new Error("Zettel: escolha a fonte da nota.");
  return choice || null;
}

// -- note flows: prompt, move the file to its final name, return the content --

async function permanent(tp) {
  const title = await ask(tp, "Titulo da nota permanente");
  const source = await pickSource(tp, true);
  const id = ulid();
  const v = { id, title, now: vaultNow() };
  if (source) Object.assign(v, { citekey: source.citekey, srcStem: source.stem });
  await tp.file.move(`30_Permanent/${noteStem("ZTL", id, title)}`);
  return source ? RENDER.permanentWithSource(v) : RENDER.permanent(v);
}

async function source(tp) {
  const title = await ask(tp, "Titulo da obra");
  const authors = (await ask(tp, "Autores, separados por ';' (Nome Sobrenome)", false))
    .split(";")
    .map((a) => a.trim())
    .filter(Boolean);
  const year = await askInt(tp, "Ano (opcional)");
  const citekey = uniqueCitekey(provisionalCitekey(authors, year, title));
  const stem = sourceNoteStem(citekey, title);
  const path = `10_Sources/${stem}.md`;
  const v = {
    citekey,
    label: authorYearLabel(citekey),
    title,
    slug: slug(title),
    authors,
    authorsText: authors.join(", ") || "Desconhecido",
    year,
    yearText: year ?? "N/A",
    path,
    now: vaultNow(),
  };
  await tp.file.move(`10_Sources/${stem}`);
  // Like `new-note src`: the SRC links to its literature index, so create it too.
  const indexPath = `20_Literature/${literatureIndexStem(citekey, title)}.md`;
  if (!app.vault.getAbstractFileByPath(indexPath)) {
    await ensureFolder("20_Literature");
    await app.vault.create(indexPath, RENDER.literatureIndex(v));
  }
  return RENDER.source(v);
}

async function literatureIndex(tp) {
  const src = await pickSource(tp, false);
  const stem = literatureIndexStem(src.citekey, src.title);
  if (app.vault.getAbstractFileByPath(`20_Literature/${stem}.md`)) {
    throw new Error(`Zettel: o indice ${stem} ja existe.`);
  }
  const v = { citekey: src.citekey, label: authorYearLabel(src.citekey), title: src.title, slug: slug(src.title), now: vaultNow() };
  await tp.file.move(`20_Literature/${stem}`);
  return RENDER.literatureIndex(v);
}

async function literature(tp) {
  const src = await pickSource(tp, false);
  const topic = await ask(tp, "Topico desta nota de literatura");
  const page = await askInt(tp, "Pagina impressa (opcional)");
  const chunkIndex = nextChunkIndex(src.citekey);
  const folder = `20_Literature/${safeComponent(src.citekey)}`;
  const v = {
    citekey: src.citekey,
    label: authorYearLabel(src.citekey),
    sourceTitle: src.title,
    sourceSlug: slug(src.title),
    topic,
    page,
    pageLabel: page === null ? "p. ?" : `p. ${page}`,
    chunkIndex,
    idx4: pad(chunkIndex, 4),
    now: vaultNow(),
  };
  await ensureFolder(folder);
  await tp.file.move(`${folder}/${literatureChunkStem(src.citekey, chunkIndex, page, topic)}`);
  return RENDER.literature(v);
}

async function moc(tp) {
  const title = await ask(tp, "Tema do MOC");
  const id = ulid();
  await tp.file.move(`40_MOCs/${noteStem("MOC", id, title)}`);
  return RENDER.moc({ id, title, now: vaultNow() });
}

const HELPERS = {
  ulid,
  slug,
  safeComponent,
  authorYearLabel,
  noteStem,
  sourceNoteStem,
  literatureIndexStem,
  literatureChunkStem,
  provisionalCitekey,
  disambiguation,
  vaultNow,
};

// Templater calls a user script as `tp.user.zettel()`; it must export a function.
module.exports = function zettel() {
  return { ...HELPERS, permanent, source, literatureIndex, literature, moc };
};
