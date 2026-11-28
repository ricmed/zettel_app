"""Reparo determinístico das cercas que o Docling parte na virada da página.

O export é um Markdown por página. Cada fatia abre e fecha as próprias cercas, o
rodapé entra no bloco, e uma linha de código vira heading. O chunker já trata a
cerca como átomo; este módulo corrige o texto antes, sem LLM e sem config.

Texto sem ``<!-- zettel:page N -->`` volta igual, então Markdown nativo não é
reescrito. A segunda passagem é no-op. O marcador de página nunca é apagado; a
cerca reunida guarda o marcador no meio.
"""

from __future__ import annotations

import re
from itertools import pairwise

from zettel.markdown_fences import iter_fenced_spans

# Hífen, en dash, em dash e sublinhado: as réguas de rodapé do PDF.
_RULER_RUN = r"[\-\u2013\u2014_]{3,}"

# Marcador numerado. O ``<!-- zettel:page-break -->`` antigo não conta: não é mapa.
_NUMBERED_PAGE_RE = re.compile(r"<!-- zettel:page \d+ -->")
_FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_ATX_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")
_COMMENT_RE = re.compile(r"^(?:#|//|/\*|\*(?:\s|$)|--(?:\s|$))")
_CODE_KEYWORD_RE = re.compile(
    r"^(?:def|class|import|from|return|for|while|if|elif|else|try|except|with|yield|assert|raise|print|@)\b"
)
_CALL_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*\s*\([^)]*\)\s*$")
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_PUNCT_RE = re.compile(r"[^\w\s'-]+", re.UNICODE)

_MIN_ECHO_CHARS = 8
_MIN_STRETCH_CHARS = 30
_MIN_PROSE_WORDS = 5


def repair_docling_code_fences(text: str) -> str:
    """Reúne a listagem cortada na página e tira a prosa do livro da cerca.

    A ordem é fixa: lixo de página, junção na virada, eco da prosa de fora,
    frase que só existe dentro, heading que repete a primeira linha da cerca.
    """
    if not text or not _NUMBERED_PAGE_RE.search(text):
        return text
    lines = text.splitlines(keepends=True)
    lines = _strip_page_junk(lines)
    lines = _join_page_split_fences(lines)
    lines = _drop_echoed_prose(lines)
    lines = _eject_book_sentences(lines)
    lines = _drop_repeated_code_headings(lines)
    repaired = "".join(lines)
    return text if repaired == text else repaired


def _strip_page_junk(lines: list[str]) -> list[str]:
    """Tira rodapé, régua e ``|`` solto de dentro da cerca e do vão entre cercas."""
    groups = _fence_groups(lines)
    if not groups:
        return lines
    drop: set[int] = set()
    for open_i, close_i in groups:
        for index in range(open_i + 1, close_i):
            if _is_page_junk(_body(lines[index])):
                drop.add(index)
    for left, right in pairwise(groups):
        for index in range(left[1] + 1, right[0]):
            if _is_page_junk(_body(lines[index])):
                drop.add(index)
    if not drop:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop]


def _join_page_split_fences(lines: list[str]) -> list[str]:
    """Duas cercas separadas só por branco, ``|`` e um marcador viram uma.

    Um heading que repete a primeira linha da cerca seguinte não é prosa: sai,
    e a listagem segue uma só. Uma frase real no vão impede a junção.
    """
    groups = _fence_groups(lines)
    if len(groups) < 2:
        return lines
    parent = list(range(len(groups)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    drop: set[int] = set()
    for index in range(len(groups) - 1):
        left_close = groups[index][1]
        right_open = groups[index + 1][0]
        gap = lines[left_close + 1 : right_open]
        ok, relative = _gap_is_page_cut(gap, lines[right_open : groups[index + 1][1] + 1])
        if not ok:
            continue
        parent[find(index + 1)] = find(index)
        drop.add(left_close)
        drop.add(right_open)
        for offset in relative:
            drop.add(left_close + 1 + offset)

    chains: dict[int, list[int]] = {}
    for index in range(len(groups)):
        chains.setdefault(find(index), []).append(index)

    replacements: dict[int, str] = {}
    for members in chains.values():
        if len(members) < 2:
            continue
        first_open = groups[members[0]][0]
        last_close = groups[members[-1]][1]
        drop.discard(first_open)
        drop.discard(last_close)
        marker = _fence_marker(lines[first_open])
        replacements[last_close] = marker + _newline(lines[last_close])

    if not drop and not replacements:
        return lines
    return [replacements.get(index, line) for index, line in enumerate(lines) if index not in drop]


def _drop_echoed_prose(lines: list[str]) -> list[str]:
    """Linha da cerca que repete o parágrafo de fora sai. A cópia de fora fica.

    Comentário e linha de código ficam, mesmo quando o texto coincide.
    """
    groups = _fence_groups(lines)
    if not groups:
        return lines
    drop: set[int] = set()
    for position, (open_i, close_i) in enumerate(groups):
        previous_end = groups[position - 1][1] + 1 if position else 0
        next_start = groups[position + 1][0] if position + 1 < len(groups) else len(lines)
        paragraphs = _paragraphs(lines[previous_end:open_i]) + _paragraphs(
            lines[close_i + 1 : next_start]
        )
        if not paragraphs:
            continue
        for index in range(open_i + 1, close_i):
            if _is_echo(lines[index], paragraphs):
                drop.add(index)
    if not drop:
        return lines
    kept = [line for index, line in enumerate(lines) if index not in drop]
    return _drop_empty_fences(kept)


def _eject_book_sentences(lines: list[str]) -> list[str]:
    """Frase de livro que só existe dentro da cerca passa para fora, partindo o bloco."""
    groups = _fence_groups(lines)
    if not groups:
        return lines
    pieces: list[str] = []
    cursor = 0
    changed = False
    for open_i, close_i in groups:
        pieces.append("".join(lines[cursor:open_i]))
        rebuilt = _fence_without_book_sentences(lines[open_i : close_i + 1])
        if rebuilt != "".join(lines[open_i : close_i + 1]):
            changed = True
        pieces.append(rebuilt)
        cursor = close_i + 1
    if not changed:
        return lines
    pieces.append("".join(lines[cursor:]))
    return "".join(pieces).splitlines(keepends=True)


def _drop_repeated_code_headings(lines: list[str]) -> list[str]:
    """``### import tqdm`` deixa de ser heading quando a cerca seguinte abre com essa linha."""
    groups = _fence_groups(lines)
    if not groups:
        return lines
    drop: set[int] = set()
    for index, line in enumerate(lines):
        if any(open_i <= index <= close_i for open_i, close_i in groups):
            continue
        match = _ATX_RE.match(_body(line).strip())
        if not match:
            continue
        following = next((group for group in groups if group[0] > index), None)
        if following is None:
            continue
        if not all(_is_gap_filler(_body(gap_line)) for gap_line in lines[index + 1 : following[0]]):
            continue
        first = _first_content_line(lines[following[0] : following[1] + 1])
        if first and _collapse(match.group(2)) == _collapse(first) and _is_code_line(first):
            drop.add(index)
    if not drop:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop]


def _fence_without_book_sentences(fence_lines: list[str]) -> str:
    if len(fence_lines) < 2:
        return "".join(fence_lines)
    opening = fence_lines[0]
    closing_nl = _newline(fence_lines[-1])
    marker = _fence_marker(opening)
    interior = fence_lines[1:-1]
    in_string = _triple_quote_mask([_body(line) for line in interior])
    segments: list[tuple[str, list[str]]] = []
    for line, quoted in zip(interior, in_string, strict=True):
        kind = "prose" if _is_book_sentence(line, quoted) else "code"
        if segments and segments[-1][0] == kind:
            segments[-1][1].append(line)
        else:
            segments.append((kind, [line]))
    if not any(kind == "prose" for kind, _segment in segments):
        return "".join(fence_lines)
    out: list[str] = []
    for kind, segment in segments:
        if kind == "prose":
            out.extend(segment)
            continue
        body = _trim_blank_edges(segment)
        if not _segment_has_content(body):
            continue
        out.append(opening if opening.endswith(("\n", "\r")) else opening + "\n")
        out.extend(body)
        out.append(marker + closing_nl)
    return "".join(out)


def _gap_is_page_cut(gap: list[str], next_fence: list[str]) -> tuple[bool, list[int]]:
    """Vão de virada de página, e os índices do vão que saem (``|``, heading repetido)."""
    if not gap:
        return False, []
    markers = 0
    drop: list[int] = []
    first = _first_content_line(next_fence)
    for index, line in enumerate(gap):
        body = _body(line).strip()
        if not body:
            continue
        if _NUMBERED_PAGE_RE.search(body):
            markers += 1
            continue
        if _is_page_junk(body):
            drop.append(index)
            continue
        match = _ATX_RE.match(body)
        if (
            match
            and first
            and _collapse(match.group(2)) == _collapse(first)
            and _is_code_line(first)
        ):
            drop.append(index)
            continue
        return False, []
    return markers == 1, drop


def _is_page_junk(body: str) -> bool:
    """Rodapé de livro (``Word Vectors | 293``, ``294 | Chapter 21``), régua ou ``|`` solto."""
    raw = body.strip()
    if not raw or _NUMBERED_PAGE_RE.search(raw):
        return False
    if re.fullmatch(r"\|+", raw) or re.fullmatch(_RULER_RUN, raw):
        return True
    # Código de verdade: pipe de shell, print, barra do tqdm. Não é rodapé.
    if any(char in raw for char in "=()[]{}$\"'`#"):
        return False
    has_ruler = bool(re.match(_RULER_RUN, raw))
    padded_pipe = bool(re.search(r"\s{2,}\||\|\s{2,}", raw))
    ends_with_page = bool(re.search(r"\|\s*\d{1,4}\s*$", raw))
    chapter = bool(re.match(r"^\d{1,4}\s*\|\s*Chapter\b", raw, re.IGNORECASE)) or bool(
        re.match(r"^\|\s*Chapter\b", raw, re.IGNORECASE)
    )
    if ends_with_page and (has_ruler or padded_pipe):
        return True
    if chapter:
        return True
    return bool(has_ruler and re.fullmatch(r"[\-\u2013\u2014_\s|]+[A-Za-z][A-Za-z ]*", raw))


def _is_echo(line: str, paragraphs: list[str]) -> bool:
    raw = _body(line).strip()
    if not raw or _NUMBERED_PAGE_RE.search(raw) or _is_comment(raw) or _is_code_line(raw):
        return False
    norm = _norm_prose(raw)
    if len(norm) < _MIN_ECHO_CHARS:
        return False
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if paragraph.startswith(norm) or paragraph.endswith(norm):
            return True
        if len(norm) >= _MIN_STRETCH_CHARS and norm in paragraph:
            return True
        # "the adic-" e o parágrafo "the adjectives": o trecho comum passa de 30
        # caracteres e o que sobra na linha é só o coto da hifenização.
        if raw.endswith("-") and _hyphen_stub_echo(norm, paragraph):
            return True
    return False


def _hyphen_stub_echo(norm: str, paragraph: str) -> bool:
    cut = 0
    limit = min(len(norm), len(paragraph))
    while cut < limit and norm[cut] == paragraph[cut]:
        cut += 1
    if cut < _MIN_STRETCH_CHARS:
        return False
    tail = norm[cut:].strip()
    return tail == "" or " " not in tail


def _is_book_sentence(line: str, in_triple_string: bool) -> bool:
    raw = _body(line).strip()
    if not raw or _NUMBERED_PAGE_RE.search(raw):
        return False
    if in_triple_string or '"""' in raw or "'''" in raw:
        return False
    if _is_comment(raw) or _is_code_line(raw):
        return False
    return len(_ALPHA_WORD_RE.findall(raw)) >= _MIN_PROSE_WORDS


def _is_code_line(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    # ``|`` que sobrou ao lixo de página é pipe de código ou de shell, não frase.
    if _CODE_KEYWORD_RE.match(stripped) or "=" in stripped or "|" in stripped:
        return True
    if stripped.startswith("$"):
        return True
    return bool(_CALL_LINE_RE.match(stripped))


def _is_comment(body: str) -> bool:
    return bool(_COMMENT_RE.match(body.strip()))


def _is_gap_filler(body: str) -> bool:
    raw = body.strip()
    return not raw or bool(_NUMBERED_PAGE_RE.search(raw)) or _is_page_junk(raw)


def _paragraphs(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        body = _body(line).strip()
        if not body or _NUMBERED_PAGE_RE.search(body) or _is_page_junk(body):
            if current:
                paragraphs.append(_norm_prose(" ".join(current)))
                current = []
            continue
        match = _ATX_RE.match(body)
        current.append(match.group(2) if match else body)
    if current:
        paragraphs.append(_norm_prose(" ".join(current)))
    return paragraphs


def _norm_prose(text: str) -> str:
    collapsed = _collapse(text)
    if collapsed.endswith("-"):
        collapsed = collapsed[:-1].rstrip()
    collapsed = _PUNCT_RE.sub(" ", collapsed)
    return _collapse(collapsed)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _triple_quote_mask(bodies: list[str]) -> list[bool]:
    state: str | None = None
    mask: list[bool] = []
    for body in bodies:
        if state is None:
            mask.append(False)
            for quote in ('"""', "'''"):
                if body.count(quote) % 2 == 1:
                    state = quote
                    break
        else:
            mask.append(True)
            if body.count(state) % 2 == 1:
                state = None
    return mask


def _first_content_line(fence_lines: list[str]) -> str:
    for line in fence_lines[1:]:
        body = _body(line).strip()
        if not body or _NUMBERED_PAGE_RE.search(body) or _is_page_junk(body):
            continue
        if _FENCE_LINE_RE.match(body):
            break
        return body
    return ""


def _segment_has_content(segment: list[str]) -> bool:
    return any(_body(line).strip() for line in segment)


def _trim_blank_edges(segment: list[str]) -> list[str]:
    start = 0
    end = len(segment)
    while start < end and not _body(segment[start]).strip():
        start += 1
    while end > start and not _body(segment[end - 1]).strip():
        end -= 1
    return segment[start:end]


def _drop_empty_fences(lines: list[str]) -> list[str]:
    groups = _fence_groups(lines)
    drop: set[int] = set()
    for open_i, close_i in groups:
        interior = lines[open_i + 1 : close_i]
        if not _segment_has_content(interior):
            drop.add(open_i)
            drop.add(close_i)
    if not drop:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop]


def _fence_groups(lines: list[str]) -> list[tuple[int, int]]:
    """Índices inclusivos (abertura, fechamento) de cada cerca CommonMark."""
    if not lines:
        return []
    text = "".join(lines)
    starts: list[int] = []
    position = 0
    for line in lines:
        starts.append(position)
        position += len(line)
    groups: list[tuple[int, int]] = []
    for start, end in iter_fenced_spans(text):
        open_i = _line_index(starts, start)
        close_i = _line_index(starts, max(start, end - 1))
        groups.append((open_i, close_i))
    return groups


def _line_index(starts: list[int], offset: int) -> int:
    index = 0
    for cursor, start in enumerate(starts):
        if start <= offset:
            index = cursor
        else:
            break
    return index


def _fence_marker(line: str) -> str:
    match = _FENCE_LINE_RE.match(_body(line))
    return match.group(1) if match else "```"


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _newline(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"
