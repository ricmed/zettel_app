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

function authorsOf(fm) {
  const raw = fm.author ?? fm.authors;
  if (Array.isArray(raw)) return raw.map((a) => String(a).trim()).filter(Boolean);
  if (typeof raw === "string" && raw.trim()) return [raw.trim()];
  return [];
}

function intOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isInteger(n) ? n : null;
}

function listSources() {
  return markdownFiles("10_Sources")
    .map((file) => {
      const fm = frontmatterOf(file);
      const sourceId = String(fm.source_id || "");
      if (!sourceId.startsWith("@")) return null;
      const citekey = String(fm.citekey || sourceId.slice(1));
      return {
        source_id: sourceId,
        citekey,
        title: String(fm.title || file.basename),
        stem: file.basename,
        authors: authorsOf(fm),
        year: intOrNull(fm.year),
      };
    })
    .filter(Boolean)
    .sort((a, b) => a.title.localeCompare(b.title));
}

function listLiterature(citekey) {
  return markdownFiles(`20_Literature/${safeComponent(citekey)}`)
    .map((file) => {
      const fm = frontmatterOf(file);
      if (String(fm.type || "") !== "literature" || !fm.chunk_id) return null;
      return {
        file,
        chunk_id: String(fm.chunk_id),
        section_path: String(fm.section_path || ""),
        page_in_book: intOrNull(fm.page_in_book),
        page_in_file: intOrNull(fm.page_in_file),
        decision_rules: fm.decision_rules,
        anti_patterns: fm.anti_patterns,
        named_frameworks: fm.named_frameworks,
      };
    })
    .filter(Boolean);
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

function assertFreshTarget(tp) {
  const path = typeof tp.file.path === "function" ? String(tp.file.path(true) || "") : "";
  if (!path) return;
  const file = app.vault.getMarkdownFiles().find((f) => f.path === path);
  if (!file) return;
  if (frontmatterOf(file).type) {
    throw new Error("Zettel: este arquivo ja e uma nota. Use 'Create new note from template', nao Insert.");
  }
}

async function pickLiterature(tp, citekey) {
  const notes = listLiterature(citekey);
  if (!notes.length) {
    throw new Error(
      `Zettel: nenhuma LIT granular em 20_Literature/${safeComponent(citekey)}/. Crie uma com o template LIT - granular.`,
    );
  }
  const labels = notes.map((n) => {
    const page = n.page_in_book ?? n.page_in_file;
    const topic = n.section_path || n.file.basename;
    return page != null ? `p. ${page} — ${topic}` : topic;
  });
  const choice = await tp.system.suggester(labels, notes, false, "Nota de literatura");
  if (!choice) throw new Error("Zettel: escolha a nota de literatura.");
  return choice;
}

async function readVault(file) {
  return String((await app.vault.read(file)) || "");
}

function fencedSpans(text) {
  const fenceLine = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  const spans = [];
  let openChar = "";
  let openLen = 0;
  let start = 0;
  let pos = 0;
  for (const line of String(text || "").split(/(?<=\n)/)) {
    const lineStart = pos;
    pos += line.length;
    const m = fenceLine.exec(line.replace(/[\r\n]+$/, ""));
    if (!m) continue;
    const marker = m[1];
    const info = m[2];
    if (openChar) {
      if (marker[0] === openChar && marker.length >= openLen && !info.trim()) {
        spans.push([start, pos]);
        openChar = "";
      }
      continue;
    }
    if (marker[0] === "`" && info.includes("`")) continue;
    openChar = marker[0];
    openLen = marker.length;
    start = lineStart;
  }
  if (openChar) spans.push([start, String(text || "").length]);
  return spans;
}

function h2Section(body, heading) {
  const text = body || "";
  const spans = fencedSpans(text);
  const re = /^##[ \t]+(.+?)[ \t]*$/gm;
  const matches = [];
  let m = re.exec(text);
  while (m) {
    if (!spans.some(([a, b]) => m.index >= a && m.index < b)) matches.push(m);
    m = re.exec(text);
  }
  for (let i = 0; i < matches.length; i++) {
    if (matches[i][1] === heading) {
      const start = matches[i].index + matches[i][0].length;
      const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
      return text.slice(start, end).trim();
    }
  }
  return "";
}

const PLACEHOLDERS = new Set([
  "_preencha o resumo._",
  "_preencha a tese._",
  "_preencha a definicao._",
  "_preencha os limites._",
  "_cole o trecho da fonte aqui._",
  "_trecho nao disponivel._",
  "_sem resumo._",
  "_nenhum._",
  "_nenhum candidato._",
  "_nenhuma._",
]);

function realText(text) {
  const value = String(text || "").trim();
  return value && !PLACEHOLDERS.has(value.toLowerCase()) ? value : "";
}

function readManagedBlock(content, name) {
  const start = `<!-- zettel:${name}:start -->`;
  const end = `<!-- zettel:${name}:end -->`;
  const i = content.indexOf(start);
  const j = content.indexOf(end);
  if (i < 0 || j < 0 || j < i) return "";
  return content.slice(i + start.length, j).trim();
}

function candidateTheses(body) {
  const raw = realText(h2Section(body, "Candidatos a Nota Permanente"));
  const theses = [];
  const re = /^\s*-\s*\[[ xX]\]\s*(.+)$/gm;
  let m = re.exec(raw);
  while (m) {
    const line = m[1].replace(/\s*<sub>.*?<\/sub>\s*$/u, "").trim().replace(/^\*+|\*+$/g, "").trim();
    if (line) theses.push(line);
    m = re.exec(raw);
  }
  return theses;
}

function keyConcepts(body) {
  const raw = realText(h2Section(body, "Conceitos-chave"));
  return [...raw.matchAll(/#([\w/-]+)/g)].map((m) => m[1]);
}

function embeddedImages(body) {
  const paths = [];
  const wiki = /!\[\[([^\]|]+?)(\|[^\]]*)?\]\]/g;
  const md = /!\[[^\]]*\]\(([^)]+)\)/g;
  let m = wiki.exec(body || "");
  while (m) {
    paths.push(m[1].trim());
    m = wiki.exec(body || "");
  }
  m = md.exec(body || "");
  while (m) {
    paths.push(m[1].trim());
    m = md.exec(body || "");
  }
  return paths.filter(Boolean);
}

function sectionTopic(sectionPath) {
  const last = String(sectionPath || "")
    .split(">")
    .pop()
    .trim();
  return ["documento completo", ""].includes(last.toLowerCase()) ? "" : last;
}

function literatureChunkTopic(sectionPath, summary, thesis) {
  if (thesis && String(thesis).trim()) return String(thesis).trim();
  if (summary && String(summary).trim()) {
    const words = String(summary).trim().split(/\s+/);
    return words.length ? words.slice(0, 8).join(" ") : "nota";
  }
  return sectionTopic(sectionPath) || "nota";
}

function formatLocator(pageInBook, sectionPath, pageInFile) {
  const parts = [];
  if (pageInBook != null) parts.push(`p.${pageInBook}`);
  else if (pageInFile != null) parts.push(`p.arquivo.${pageInFile}`);
  if (sectionPath) parts.push(sectionPath);
  return parts.join(" / ");
}

function authorSurname(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1].toUpperCase() : "";
}

function formatAbnt(authors, year, pages) {
  const cleaned = (authors || []).map((a) => String(a).trim()).filter(Boolean);
  const yearStr = year != null && year !== "" ? String(year) : "s.d.";
  if (!cleaned.length) return year != null && year !== "" ? `(${yearStr})` : "";
  let citeAuthors;
  if (cleaned.length === 1) citeAuthors = authorSurname(cleaned[0]);
  else if (cleaned.length === 2) citeAuthors = `${authorSurname(cleaned[0])}; ${authorSurname(cleaned[1])}`;
  else if (cleaned.length === 3) {
    citeAuthors = `${authorSurname(cleaned[0])}; ${authorSurname(cleaned[1])}; ${authorSurname(cleaned[2])}`;
  } else citeAuthors = `${authorSurname(cleaned[0])} et al.`;
  if (pages && String(pages).trim()) return `(${citeAuthors}, ${yearStr}, ${String(pages).trim()})`;
  return `(${citeAuthors}, ${yearStr})`;
}

function literatureRef(file, page, topic) {
  const rel = String(file.path || "")
    .replace(/^20_Literature\//, "")
    .replace(/\.md$/, "");
  const alias = page != null ? `p. ${page} — ${topic}` : topic;
  return `[[${rel}|${alias}]]`;
}

function evidenceBlock(citation, quote) {
  const lines = [];
  if (citation) lines.push(`- Citação: ${citation}`);
  if (quote) {
    const suffix = citation ? ` ${citation}` : "";
    lines.push(`\n- Trecho literal:\n\n> "${quote}"${suffix}\n`);
  }
  if (!lines.length) return "";
  return `\n\n<!-- zettel:auto-evidence:start -->\n${lines.join("\n")}\n<!-- zettel:auto-evidence:end -->`;
}

function figuresSection(paths) {
  if (!paths.length) return "";
  return `## Figuras\n\n${paths.map((p) => `![[${p}]]`).join("\n\n")}\n\n`;
}

function judgementList(raw) {
  if (!Array.isArray(raw)) return undefined;
  const values = raw.map((item) => String(item).trim()).filter(Boolean).slice(0, 3);
  return values.length ? values : undefined;
}

function deriveCandidate(content, meta, thesisOverride) {
  const summary = realText(h2Section(content, "Resumo"));
  const theses = candidateTheses(content);
  let thesis = String(thesisOverride || "").trim() || theses[0] || "";
  if (!thesis && summary) thesis = summary.split("\n\n")[0].trim();
  if (!thesis) {
    throw new Error(
      "Nao foi possivel derivar uma tese da nota de literatura: preencha " +
        "'## Resumo' ou '## Candidatos a Nota Permanente', ou informe --thesis.",
    );
  }
  const quote = realText(readManagedBlock(content, "auto-source-excerpt"));
  return {
    thesis,
    definition: summary || thesis,
    anchorQuote: quote,
    tags: keyConcepts(content),
    sourceLocator: formatLocator(meta.page_in_book, meta.section_path || "", meta.page_in_file),
  };
}

// -- note flows: prompt, move the file to its final name, return the content --

async function permanent(tp) {
  assertFreshTarget(tp);
  const src = await pickSource(tp, false);
  const lit = await pickLiterature(tp, src.citekey);
  const thesisOverride = await ask(tp, "Tese (Enter para derivar da LIT)", false);
  const content = await readVault(lit.file);
  const candidate = deriveCandidate(content, lit, thesisOverride);
  const definition = candidate.definition !== candidate.thesis ? candidate.definition : "";
  const title = candidate.thesis.slice(0, 100);
  const page = lit.page_in_book ?? lit.page_in_file;
  const pages = page != null ? `p. ${page}` : "";
  const citation = formatAbnt(src.authors, src.year, pages);
  const topic = literatureChunkTopic(lit.section_path, realText(h2Section(content, "Resumo")), candidate.thesis);
  const id = ulid();
  const v = {
    id,
    title,
    thesis: candidate.thesis,
    definition: definition || "_Preencha a definicao._",
    citekey: src.citekey,
    srcStem: src.stem,
    literatureRef: literatureRef(lit.file, page, topic),
    chunkId: lit.chunk_id,
    tags: candidate.tags,
    page: page ?? undefined,
    pageLine: page != null ? `\n- Página: ${page}` : "",
    pageConfidence: page != null ? "chunk" : undefined,
    locatorLine: candidate.sourceLocator ? `\n- Localizador: ${candidate.sourceLocator}` : "",
    evidenceBlock: evidenceBlock(citation, candidate.anchorQuote),
    figuresSection: figuresSection(embeddedImages(content)),
    decisionRules: judgementList(lit.decision_rules),
    antiPatterns: judgementList(lit.anti_patterns),
    namedFrameworks: judgementList(lit.named_frameworks),
    now: vaultNow(),
  };
  await tp.file.move(`30_Permanent/${noteStem("ZTL", id, title)}`);
  return RENDER.permanentFromLit(v);
}

async function source(tp) {
  assertFreshTarget(tp);
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
  assertFreshTarget(tp);
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
  assertFreshTarget(tp);
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
  assertFreshTarget(tp);
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
  formatAbnt,
  formatLocator,
  literatureChunkTopic,
  evidenceBlock,
  figuresSection,
};

// Templater calls a user script as `tp.user.zettel()`; it must export a function.
module.exports = function zettel() {
  return { ...HELPERS, permanent, source, literatureIndex, literature, moc };
};
