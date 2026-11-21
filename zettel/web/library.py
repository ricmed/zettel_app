"""Estante: a ficha é o SRC, com lista, painel, detalhe e notas de leitura.

Rotas em inglês (``/books``), página em PT-BR. Toda edição é um POST de
formulário com CSRF, feito sob a trava de operação e conferido contra a versão
do arquivo que a página mostrou: uma edição no Obsidian nesse meio-tempo dá 409,
nunca é sobrescrita. O espelho SQLite é posto em dia no início de cada página.
"""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from zettel.markdown import asset_url
from zettel.web.enqueue import operation_lock, post_job
from zettel.web.forms import read_form
from zettel.web.mesa import desk_choices
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

LAYOUTS = ("table", "gallery", "board")
SORTS = {
    "code": "Código",
    "title": "Título",
    "score": "Nota",
    "finished": "Término",
    "added": "Inclusão",
}
EDIT_ACTIONS = (
    "metadata",
    "bibliography",
    "reading-start",
    "reading-finish",
    "reading-remove",
    "rating",
    "summary",
    "status",
    "cover",
    "cover-isbn",
    "complete",
    "link-work",
    "unlink-work",
)
_LOCK = "estante"
# O que um candidato da pesquisa pode pré-preencher no formulário de cadastro.
PREFILL_KEYS = (
    "title",
    "original_title",
    "isbn",
    "pages",
    "original_publication_year",
    "publisher",
    "place",
    "year",
    "cover_ref",
    "country",
    "original_language",
    "document_type",
    "doi",
    "journal",
    "volume",
    "issue",
)


def _today(cfg: Any) -> datetime.date:
    from zoneinfo import ZoneInfo

    return datetime.datetime.now(ZoneInfo(cfg.vault_timezone)).date()


def _open(request: Request) -> tuple[Any, Any]:
    """``(cfg, db)`` com o espelho da Estante em dia; quem chama fecha o db."""
    from zettel.library.mirror import refresh

    svc = service(request)
    db = svc.db()
    refresh(db, svc.cfg)
    return svc.cfg, db


def _book_card(row: dict[str, Any]) -> dict[str, Any]:
    import json

    from zettel.library.book_file import FORMAT_LABELS, STATUS_LABELS

    return {
        **row,
        "authors": json.loads(row.get("authors_json") or "[]"),
        "genres": json.loads(row.get("genres_json") or "[]"),
        "cover_url": asset_url(row.get("cover")),
        "status_label": STATUS_LABELS.get(row.get("status") or "", "—"),
        "format_label": FORMAT_LABELS.get(row.get("format") or "", "—"),
    }


def _type_labels(cfg: Any) -> dict[str, str]:
    from zettel.bibliography import DOCUMENT_TYPE_LABELS

    return {
        key: DOCUMENT_TYPE_LABELS[key]
        for key in cfg.library.document_types
        if key in DOCUMENT_TYPE_LABELS
    }


def _filters(request: Request) -> dict[str, Any]:
    q = request.query_params

    def number(key: str, cast):
        try:
            return cast(q.get(key)) if q.get(key) else None
        except ValueError:
            return None

    return {
        "status": q.get("status", ""),
        "genre": q.get("genre", ""),
        "nature": q.get("nature", ""),
        "format": q.get("format", ""),
        "country": q.get("country", ""),
        "reading_mode": q.get("reading_mode", ""),
        "year": number("year", int),
        "min_score": number("min_score", float),
        "query": q.get("q", "")[:200],
        "favorite": q.get("favorite") == "1",
        "document_type": q.get("document_type", ""),
        "sort": q.get("sort", "code") if q.get("sort") in SORTS else "code",
    }


@router.get("/books", response_class=HTMLResponse)
def books_list(request: Request):
    if not authenticated(request):
        return redirect_login()
    from zettel.library.book_file import FORMAT_LABELS, NATURE_LABELS, STATUS_LABELS, STATUSES
    from zettel.library.schema import LibrarySchemaError, schema_for

    filters = _filters(request)
    layout = request.query_params.get("layout", "table")
    layout = layout if layout in LAYOUTS else "table"
    try:
        cfg, db = _open(request)
    except LibrarySchemaError as exc:
        return error_page(request, 500, f"Vocabulário da Estante inválido: {exc}")
    try:
        types = list(cfg.library.document_types)
        books = [_book_card(r) for r in db.list_shelf_rows(types, **filters)]
        total = len(db.list_shelf_identities(types))
    finally:
        db.close()
    board = [
        {"status": s, "label": STATUS_LABELS[s], "books": [b for b in books if b["status"] == s]}
        for s in STATUSES
    ]
    codes = [b["code_number"] for b in books if b.get("code_number")]
    duplicated = sorted({c for c in codes if codes.count(c) > 1})
    return render(
        request,
        "books.html",
        page="library",
        books=books,
        board=board,
        total=total,
        filters=filters,
        layout=layout,
        sorts=SORTS,
        duplicated_codes=duplicated,
        genre_groups=schema_for(cfg).grouped_genres(
            sorted({g for b in books for g in b["genres"]})
        ),
        status_labels=STATUS_LABELS,
        format_labels=FORMAT_LABELS,
        nature_labels=NATURE_LABELS,
        type_labels=_type_labels(cfg),
        this_year=_today(cfg).year,
    )


@router.get("/books/dashboard", response_class=HTMLResponse)
def books_dashboard(request: Request):
    if not authenticated(request):
        return redirect_login()
    from zettel.library.schema import schema_for
    from zettel.library.stats import build_dashboard

    cfg, db = _open(request)
    try:
        raw = request.query_params.get("year", "")
        year = int(raw) if raw.isdecimal() and len(raw) == 4 else _today(cfg).year
        stats = build_dashboard(db, schema_for(cfg), year, list(cfg.library.document_types))
    finally:
        db.close()
    return render(request, "books_dashboard.html", page="library", stats=stats)


def _shelf_match(identities: list[dict[str, Any]], candidate: Any) -> dict[str, Any] | None:
    """O livro da estante que já é este candidato: mesmo ISBN, ou título + sobrenome."""
    import json

    from zettel.harvester.biblio_dedupe import normalize_isbn
    from zettel.library.linking import same_work

    as_source = {"title": candidate.title, "authors": json.dumps(candidate.authors)}
    for row in identities:
        if candidate.isbn and normalize_isbn(row.get("isbn")) == candidate.isbn:
            return row
    return next((row for row in identities if same_work(row, as_source)), None)


def _prefill(request: Request) -> dict[str, str]:
    q = request.query_params
    values = {k: q.get(k, "")[:300].strip() for k in PREFILL_KEYS if q.get(k)}
    authors = [a.strip()[:300] for a in q.getlist("authors_list") if a.strip()]
    if authors:
        values["authors"] = "\n".join(authors[:10])
    return values


@router.get("/books/new", response_class=HTMLResponse)
def book_new_page(request: Request):
    """Cadastro em dois passos: pesquisar (ISBN ou título) e conferir o formulário.

    ``?q=`` pesquisa no Google Books, ou no serviço de ``?provider=`` (o usuário
    refaz a busca na Open Library quando a lista não serve). ``?step=form``
    mostra o formulário, pré-preenchido pelo candidato escolhido (os campos vêm
    na query string) ou vazio. Com a pesquisa desligada na configuração, a
    página já abre no formulário.
    """
    if not authenticated(request):
        return redirect_login()
    from zettel.harvester.biblio_dedupe import normalize_doi
    from zettel.library.book_file import FORMAT_LABELS, STATUS_LABELS
    from zettel.library.lookup import (
        PROVIDER_ARTICLES,
        PROVIDER_LABELS,
        PROVIDERS,
        BookLookupError,
        lookup_doi,
        search_books,
    )
    from zettel.library.schema import schema_for

    cfg, db = _open(request)
    try:
        identities = db.list_shelf_identities(list(cfg.library.document_types))
        codes = {r["source_id"]: r.get("code_number") for r in identities}
    finally:
        db.close()
    online = cfg.library.online_lookup
    query = " ".join(request.query_params.get("q", "").split())[:200]
    step = "form" if request.query_params.get("step") == "form" or not online else "search"
    provider = request.query_params.get("provider", PROVIDERS[0])
    provider = provider if provider in PROVIDERS else PROVIDERS[0]
    results: list[dict[str, Any]] = []
    lookup_error = ""
    doi_work = None
    doi = normalize_doi(query) if step == "search" and query else ""
    if doi and online:
        # DOI pergunta ao Crossref, não ao Google Books. Nada é gravado neste GET.
        try:
            doi_work = lookup_doi(doi)
        except BookLookupError as exc:
            lookup_error = str(exc)
        if doi_work is None and not lookup_error:
            lookup_error = "O Crossref não devolveu esta obra. O formulário manual continua à mão."
    elif step == "search" and query:
        # Só leitura e fora da trava: nada é gravado até o formulário ser enviado.
        try:
            found = search_books(query, provider)
        except BookLookupError as exc:
            found, lookup_error = [], str(exc)
        for cand in found:
            match = _shelf_match(identities, cand)
            results.append(
                {
                    "c": cand,
                    "fields": cand.form_fields(),
                    # Sugestão pelas categorias do catálogo; o usuário confere no formulário.
                    "genres": schema_for(cfg).genres_for_categories(cand.categories),
                    "on_shelf": match,
                    "code": codes.get(match["source_id"]) if match else None,
                }
            )
    schema = schema_for(cfg)
    prefill = _prefill(request) if step == "form" else {}
    prefill_genres = [g[:80] for g in request.query_params.getlist("genres") if g.strip()][:12]
    work, work_error = None, ""
    if step == "form" and online and prefill.get("title") and not prefill.get("doi"):
        work, work_error = _work_facts(
            prefill["title"],
            prefill.get("authors", "").splitlines(),
            prefill.get("original_title", ""),
        )
        if work:
            # A obra (Wikidata) vale mais que o "ano da obra" do catálogo de edições,
            # que costuma ser o de uma edição antiga; o que já veio digitado fica.
            facts = work.book_fields()
            for key, value in facts.items():
                if key == "original_publication_year" or not prefill.get(key):
                    prefill[key] = str(value)
    return render(
        request,
        "book_new.html",
        page="library",
        step=step,
        online=online,
        query=query,
        results=results,
        lookup_error=lookup_error,
        doi_work=doi_work,
        provider=provider,
        provider_label=PROVIDER_LABELS[provider],
        provider_article=PROVIDER_ARTICLES[provider],
        other_providers=[
            (p, PROVIDER_ARTICLES[p], PROVIDER_LABELS[p]) for p in PROVIDERS if p != provider
        ],
        prefill=prefill,
        work=work,
        work_error=work_error,
        genre_groups=schema.grouped_genres(prefill_genres),
        prefill_genres=prefill_genres,
        subject_options=schema.subjects,
        format_labels=FORMAT_LABELS,
        status_labels=STATUS_LABELS,
        type_labels=_type_labels(cfg),
        initial_status=cfg.library.initial_status,
    )


@router.get("/books/lookup/cover")
def book_lookup_cover(request: Request):
    """A miniatura de um candidato, servida daqui para a CSP seguir só com ``img-src 'self'``."""
    if not authenticated(request):
        return Response(status_code=401)
    from zettel.library.covers import CoverError, fetch_cover_ref

    if not service(request).cfg.library.online_lookup:
        return Response(status_code=404)
    try:
        data, suffix = fetch_cover_ref(request.query_params.get("ref", ""), large=False)
    except CoverError:
        return Response(status_code=404)
    media = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}.get(
        suffix, "image/webp"
    )
    return Response(data, media_type=media, headers={"Cache-Control": "private, max-age=86400"})


def _work_facts(title: str, authors: list[str], original_title: str = "") -> tuple[Any, str]:
    """``(WorkFacts | None, erro)``: o Wikidata fora do ar não impede o cadastro."""
    from zettel.library.lookup import BookLookupError
    from zettel.library.wikidata import work_facts

    try:
        authors = [a for a in authors if a.strip()]
        return work_facts(title, authors, original_title=original_title), ""
    except BookLookupError as exc:
        return None, str(exc)


def _search_cover(cfg: Any, ref: str) -> tuple[bytes, str] | None:
    """A capa grande de um candidato; uma falha não impede o cadastro (o detalhe tem o botão)."""
    import logging

    from zettel.library.covers import CoverError, fetch_cover_ref

    if not ref or not cfg.library.online_lookup:
        return None
    try:
        return fetch_cover_ref(ref, large=True)
    except CoverError as exc:
        logging.getLogger(__name__).info("Capa da pesquisa não baixada (%s): %s", ref, exc)
        return None


@router.post("/books")
def book_create(request: Request, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.library.book_file import BookFileBusy, create_book, save_book
    from zettel.library.covers import store_cover
    from zettel.library.editing import BookEditError, apply_metadata, set_cover
    from zettel.library.mirror import refresh
    from zettel.library.schema import schema_for
    from zettel.library.sources import update_source
    from zettel.oplock import LockBusy
    from zettel.sync import sync_source_note

    cfg = service(request).cfg
    values = _form_values(form)
    try:
        source_updates = _source_updates(values)
    except BookEditError as exc:
        return error_page(request, 400, str(exc))
    for key in ("doi", "journal", "volume", "issue", "document_type"):
        text = " ".join(str(values.get(key) or "").split())[:300]
        if text:
            source_updates[key] = text
    # A rede fica fora da trava: só a gravação precisa dela.
    cover = _search_cover(cfg, str(values.get("cover_ref") or ""))
    try:
        lock = operation_lock(request, _LOCK)
    except LockBusy as busy:
        return error_page(request, 409, f"O vault está em uso. {busy}")
    db = service(request).db()
    try:
        if not str(values.get("title") or "").strip():
            return error_page(request, 400, "Dê um título ao livro.")
        try:
            authors = [
                line.strip()
                for line in str(values.get("authors") or "").splitlines()
                if line.strip()
            ]
            book = create_book(
                cfg,
                {
                    "title": values["title"],
                    "authors": authors,
                    "format": values.get("format"),
                    "status": values.get("status"),
                    "document_type": values.get("document_type") or "livro",
                    "isbn": values.get("isbn") or None,
                    "year": source_updates.get("year"),
                },
                db=db,
            )
        except BookFileBusy as exc:
            return error_page(request, 409, f"A nota não foi criada. {exc}")
        try:
            apply_metadata(book, {**_metadata_defaults(book), **values}, schema_for(cfg))
        except BookEditError as exc:
            book.path.unlink(missing_ok=True)
            return error_page(request, 400, str(exc))
        if any(source_updates.values()):
            update_source(cfg, db, book, source_updates)
        if cover:
            set_cover(book, store_cover(cfg.vault_path, *cover))
        try:
            book = save_book(cfg, book, expected_version=book.version)
        except BookFileBusy as exc:
            return error_page(
                request,
                409,
                f"A ficha «{book.title}» ({book.code}) ficou só com o título. {exc} "
                "Enviar o formulário outra vez cria um segundo livro. Feche a nota e "
                "complete a ficha na Estante, por esse título.",
            )
        sync_source_note(cfg, db, book.path)
        refresh(db, cfg)
    finally:
        db.close()
        lock.release()
    return RedirectResponse(f"/books/{book.source_id}", status_code=303)


def _form_values(form: FormData) -> dict[str, Any]:
    """O formulário como dict; ``genres`` (checkboxes) vira lista."""
    values: dict[str, Any] = {k: v for k, v in form.items() if isinstance(v, str)}
    for key in ("genres", "subjects"):
        values[key] = [str(v) for v in form.getlist(key) if isinstance(v, str)]
    return values


def _metadata_defaults(book: Any) -> dict[str, Any]:
    """O que o formulário de metadados mandaria para um livro recém-criado."""
    return {
        "title": book.title,
        "format": book.format,
        "status": book.status,
        "code_number": str(book.meta.get("code_number") or ""),
    }


def _bibliography(source: dict[str, Any] | None) -> dict[str, Any]:
    import json

    if not source:
        return {}
    try:
        biblio = json.loads(source.get("bibliography_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return biblio if isinstance(biblio, dict) else {}


def _work_siblings(db: Any, source_id: str) -> list[dict[str, str]]:
    from zettel.library.works import sibling_views

    return sibling_views(db, source_id)


def _work_choices(cfg: Any, db: Any, source_id: str) -> list[dict[str, str]]:
    from zettel.library.works import shelf_choices

    return shelf_choices(cfg, db, source_id)


def _note_search(request: Request, source_id: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Totais da obra e a página de 10 da busca (tipo LIT, ZTL ou ambas)."""
    from zettel.library.linking import (
        match_work_notes,
        page_work_notes,
        work_note_rows,
        work_note_totals,
    )

    lit_total, ztl_total = work_note_totals(groups)
    query = (request.query_params.get("q") or "").strip()[:200]
    tipo = request.query_params.get("tipo") or "ambas"
    if tipo not in ("lit", "ztl", "ambas"):
        tipo = "ambas"
    raw_page = request.query_params.get("pagina") or "1"
    page = int(raw_page) if raw_page.isdecimal() and len(raw_page) <= 6 and int(raw_page) > 0 else 1
    matched = match_work_notes(work_note_rows(groups), query=query, kind=tipo)
    visible, current, pages = page_work_notes(matched, page=page)
    own_note_ids = [
        note["note_id"] for group in groups if group.get("own") for note in group["notes"]
    ]

    def page_url(number: int) -> str:
        params: dict[str, str] = {}
        if query:
            params["q"] = query
        if tipo != "ambas":
            params["tipo"] = tipo
        if number > 1:
            params["pagina"] = str(number)
        encoded = urlencode(params)
        target = f"/books/{source_id}"
        return f"{target}?{encoded}#notas" if encoded else f"{target}#notas"

    return {
        "lit_total": lit_total,
        "ztl_total": ztl_total,
        "note_query": query,
        "note_tipo": tipo,
        "note_matched": len(matched),
        "note_page": visible,
        "note_current": current,
        "note_pages": pages,
        "note_previous_url": page_url(current - 1) if current > 1 else None,
        "note_next_url": page_url(current + 1) if current < pages else None,
        "own_note_ids": own_note_ids,
        "work_linked": any(group.get("heading") for group in groups),
        "show_llm_hint": any(item["own"] and item["kind"] == "lit" for item in visible),
    }


def _source_view(db: Any, source_id: str | None) -> dict[str, Any]:
    from zettel.library.linking import note_groups

    if not source_id:
        return {"source": None, "lits": [], "notes": [], "note_groups": []}
    source = db.get_source(source_id)
    if not source:
        return {
            "source": None,
            "missing_source": source_id,
            "lits": [],
            "notes": [],
            "note_groups": [],
        }
    groups = note_groups(db, source_id)
    own = next((group for group in groups if group["own"]), None) or {
        "lits": [],
        "notes": [],
    }
    return {
        "source": source,
        "lits": own["lits"],
        "notes": own["notes"],
        "note_groups": groups,
    }


@router.get("/books/{source_id}", response_class=HTMLResponse)
def book_detail(request: Request, source_id: str):
    return _book_page(request, source_id)


def _book_page(
    request: Request,
    source_id: str,
    *,
    draft: dict[str, str] | None = None,
    edition_notice: dict[str, Any] | None = None,
):
    if not authenticated(request):
        return redirect_login()
    from zettel.credentials import llm_phase_ready
    from zettel.library import book_file as bf
    from zettel.library.book_file import as_str_list
    from zettel.library.covers import OPEN_LIBRARY_HOME
    from zettel.library.rating import rate
    from zettel.library.reading_mode import effective_nature, effective_reading_mode
    from zettel.library.reading_notes import REFLECTION_KINDS
    from zettel.library.schema import schema_for

    cfg, db = _open(request)
    try:
        try:
            book = bf.load_book(cfg, source_id)
        except bf.BookNotFound:
            return error_page(request, 404, "Livro não encontrado.")
        schema = schema_for(cfg)
        row = db.get_source(source_id) or {}
        view = _source_view(db, book.source_id)
        chunks = db.get_chunks_for_source(book.source_id) if view.get("source") else []
        siblings = _work_siblings(db, book.source_id)
        choices = _work_choices(cfg, db, book.source_id)
    finally:
        db.close()
    rating = rate(schema, book.meta)
    m_subjects = book.meta.get("subjects")
    # O que "Automático" daria agora: a natureza só pelos gêneros; o modo pela
    # natureza (escolhida ou automática) e pelos gêneros reflexivos.
    auto_nature = effective_nature({**book.meta, "nature": None}, schema)
    auto_mode = effective_reading_mode({**book.meta, "reading_mode": None}, schema)
    biblio = _bibliography(view.get("source"))
    summary = bf.section_text(book.body, bf.SUMMARY_HEADING)
    impressions = bf.section_text(book.body, bf.IMPRESSIONS_HEADING)
    return render(
        request,
        "book_detail.html",
        page="library",
        book=book,
        row=row,
        cover_url=asset_url(book.meta.get("cover")),
        rating=rating,
        schema=schema,
        summary=summary,
        impressions=impressions,
        readings=book.readings,
        open_reading=bool(book.readings and book.readings[-1].start and not book.readings[-1].end),
        status_labels=bf.STATUS_LABELS,
        format_labels=bf.FORMAT_LABELS,
        nature_labels=bf.NATURE_LABELS,
        mode_labels=bf.READING_MODE_LABELS,
        auto_nature=auto_nature,
        auto_mode=auto_mode,
        reflection_kinds=REFLECTION_KINDS,
        genre_groups=schema.grouped_genres(book.genres),
        subject_options=list(dict.fromkeys([*schema.subjects, *as_str_list(m_subjects)])),
        online_lookup=cfg.library.online_lookup,
        open_library_home=OPEN_LIBRARY_HOME,
        connect_llm_ready=llm_phase_ready(cfg, "connect"),
        desks=desk_choices(request),
        completed=_completed_notice(request),
        chunks=chunks,
        siblings=siblings,
        shelf_choices=choices,
        draft=draft or {},
        edition_notice=edition_notice,
        document_label=_type_labels(cfg).get(
            str(
                (view.get("source") or {}).get("document_type")
                or book.meta.get("document_type")
                or ""
            ),
            "",
        ),
        biblio=biblio,
        today=_today(cfg).isoformat(),
        **view,
        **_note_search(request, book.source_id, view.get("note_groups") or []),
    )


COMPLETED_LABELS = {
    "authors": "autores",
    "isbn": "ISBN",
    "pages": "páginas",
    "cover": "capa",
    "country": "país do autor",
    "original_language": "idioma original",
    "original_title": "título original",
    "original_publication_year": "ano da 1ª publicação",
    "publisher": "editora",
    "place": "local",
    "year": "ano desta edição",
}


def _completed_notice(request: Request) -> dict[str, Any] | None:
    """O resultado de "Completar dados", lido da query do redirect (só códigos conhecidos)."""
    raw = request.query_params.get("completed")
    if raw is None:
        return None
    filled = [COMPLETED_LABELS[k] for k in raw.split(",") if k in COMPLETED_LABELS]
    work = request.query_params.get("work", "none")
    return {"filled": filled, "work": work if work in ("work", "author", "none") else "none"}


def _change(action: str, values: dict[str, Any], form: FormData, cfg: Any, schema: Any):
    """A edição pedida por ``action``, como função do livro (e do db)."""
    from zettel.library import editing as ed
    from zettel.library.covers import store_cover
    from zettel.library.sources import update_source

    today = _today(cfg)
    if action == "metadata":
        return lambda book, db: ed.apply_metadata(book, values, schema)
    if action == "reading-start":
        return lambda book, db: ed.start_reading(book, values.get("date"), today)
    if action == "reading-finish":
        return lambda book, db: ed.finish_reading(book, values.get("date"), today)
    if action == "reading-remove":
        return lambda book, db: ed.remove_reading(book, values.get("position"))
    if action == "rating":
        return lambda book, db: ed.set_ratings(book, values, schema)
    if action == "summary":
        return lambda book, db: ed.set_texts(book, values.get("summary"), values.get("impressions"))
    if action == "status":
        return lambda book, db: ed.set_status(book, str(values.get("status") or ""))
    if action == "bibliography":
        updates = _source_updates(values)
        return lambda book, db: update_source(cfg, db, book, updates)
    if action == "cover":
        cover = _uploaded_cover(form)
        return lambda book, db: ed.set_cover(book, store_cover(cfg.vault_path, *cover))
    if action == "link-work":
        other_id = str(values.get("other_id") or "")

        def change(book, db, other_id=other_id):
            from zettel.library.works import WorkLinkError, link_editions

            try:
                link_editions(cfg, db, book.source_id, other_id)
            except WorkLinkError as exc:
                raise ed.BookEditError(str(exc)) from exc
            return "saved"

        return change
    if action == "unlink-work":

        def change(book, db):
            from zettel.library.works import WorkLinkError, unlink_edition

            try:
                unlink_edition(cfg, db, book.source_id)
            except WorkLinkError as exc:
                raise ed.BookEditError(str(exc)) from exc
            return "saved"

        return change
    raise ed.BookEditError("Ação desconhecida.")


def _uploaded_cover(form: FormData) -> tuple[bytes, str]:
    from zettel.library.editing import BookEditError
    from zettel.web.manual_images import read_image_uploads

    try:
        uploads = read_image_uploads(form)
    except ValueError as exc:
        raise BookEditError(str(exc)) from exc
    if len(uploads) != 1:
        raise BookEditError("Envie uma imagem de capa.")
    name, data = uploads[0]
    return data, "." + name.rsplit(".", 1)[-1].lower()


def _source_updates(values: dict[str, Any]) -> dict[str, Any]:
    """Os campos da edição de um formulário, validados; vazio é ``""``/``None``."""
    from zettel.library.editing import BookEditError
    from zettel.library.sources import SOURCE_KEYS

    updates: dict[str, Any] = {
        key: " ".join(str(values.get(key) or "").split())[:300] for key in SOURCE_KEYS
    }
    year = str(values.get("year") or "").strip()
    if year and not (year.isdecimal() and len(year) == 4):
        raise BookEditError("Ano inválido.")
    updates["year"] = int(year) if year else None
    return updates


def _edit(
    request: Request,
    source_id: str,
    version: str | None,
    change,
    anchor: str = "",
    query: Any = None,
):
    """Carrega, muda e grava um livro sob a trava; o espelho é posto em dia em seguida.

    ``query`` (opcional) é chamado depois da gravação e devolve a query string
    do redirect: é como "Completar dados" conta à página o que preencheu.
    """
    from zettel.library import book_file as bf
    from zettel.library.covers import CoverError
    from zettel.library.editing import BookEditError
    from zettel.library.linking import write_book_notes_block
    from zettel.library.mirror import refresh
    from zettel.library.reading_notes import ReadingNoteError
    from zettel.oplock import LockBusy
    from zettel.sync import sync_source_note

    cfg = service(request).cfg
    try:
        lock = operation_lock(request, _LOCK)
    except LockBusy as busy:
        return error_page(request, 409, f"O vault está em uso. {busy}")
    db = service(request).db()
    try:
        book = bf.load_book(cfg, source_id)
        if version is not None and book.version != version:
            raise bf.BookConflict(source_id)
        if change(book, db) != "saved":
            bf.save_book(cfg, book, expected_version=book.version)
        saved = bf.load_book(cfg, source_id)
        sync_source_note(cfg, db, saved.path)
        refresh(db, cfg)
        saved = bf.load_book(cfg, source_id)
        write_book_notes_block(cfg, db, saved)
        refresh(db, cfg)
    except bf.BookNotFound:
        return error_page(request, 404, "Livro não encontrado.")
    except bf.BookConflict:
        return error_page(
            request,
            409,
            "O livro mudou desde que esta página foi aberta (talvez no Obsidian). "
            "Recarregue a página e refaça a alteração.",
        )
    except bf.BookFileBusy as exc:
        return error_page(
            request,
            409,
            f"{exc} Feche a nota e repita a alteração. O arquivo no disco continua como estava.",
        )
    except (BookEditError, CoverError, ReadingNoteError) as exc:
        return error_page(request, 400, str(exc))
    finally:
        db.close()
        lock.release()
    suffix = query() if query else ""
    return RedirectResponse(
        f"/books/{source_id}" + (f"?{suffix}" if suffix else "") + (f"#{anchor}" if anchor else ""),
        303,
    )


@router.post("/books/{source_id}")
def book_edit(request: Request, source_id: str, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.library.covers import CoverError, fetch_open_library_cover, store_cover
    from zettel.library.editing import BookEditError, set_cover
    from zettel.library.lookup import BookLookupError
    from zettel.library.schema import schema_for

    cfg = service(request).cfg
    action = str(form.get("action") or "")
    if action not in EDIT_ACTIONS:
        return error_page(request, 400, "Ação desconhecida.")
    values = _form_values(form)
    try:
        if action in ("cover-isbn", "complete") and not cfg.library.online_lookup:
            return error_page(request, 409, "A pesquisa online está desligada na configuração.")
        report: dict[str, Any] = {}
        if action == "complete":
            change = _complete_change(cfg, _load_or_none(cfg, source_id), report)
        elif action == "cover-isbn":
            # A rede fica fora da trava: só a gravação precisa dela.
            data, suffix = fetch_open_library_cover(str(values.get("isbn") or ""))

            def change(book, db):
                return set_cover(book, store_cover(cfg.vault_path, data, suffix))
        else:
            change = _change(action, values, form, cfg, schema_for(cfg))
    except (BookEditError, CoverError, BookLookupError) as exc:
        return error_page(request, 400, str(exc))
    anchor = {"rating": "avaliacao", "summary": "resumo"}.get(action, "")
    if action.startswith("reading-"):
        anchor = "leituras"
    if action in ("link-work", "unlink-work"):
        anchor = "obra"
    query = None
    if action == "complete":
        from urllib.parse import urlencode

        def query():
            return urlencode(
                {"completed": ",".join(report["filled"]) or "-", "work": report["work"]}
            )

    return _edit(request, source_id, str(form.get("version") or ""), change, anchor, query)


def _load_or_none(cfg: Any, source_id: str):
    from zettel.library import book_file as bf

    try:
        return bf.load_book(cfg, source_id)
    except bf.BookNotFound:
        return None


def _complete_change(cfg: Any, book: Any, report: dict[str, Any]):
    """Completar dados: pesquisa agora (fora da trava) e preenche só o que está vazio.

    Pelo ISBN (Google Books, depois Open Library): autores, páginas, capa e a
    edição. Pelo DOI (Crossref): periódico, volume, fascículo, páginas, editora
    e ano, só onde a ficha está vazia. Pelo Wikidata (título + autores): país do
    autor, idioma, título e ano originais. Basta uma das fontes responder.
    """
    from zettel.harvester.biblio_dedupe import normalize_doi
    from zettel.library.covers import store_cover
    from zettel.library.editing import BookEditError, fill_missing, set_cover
    from zettel.library.lookup import BookLookupError, lookup_doi, lookup_isbn
    from zettel.library.sources import update_source

    if book is None:
        raise BookEditError("Livro não encontrado.")
    errors: list[str] = []
    found = None
    if book.meta.get("isbn"):
        try:
            found = lookup_isbn(str(book.meta["isbn"]))
        except BookLookupError as exc:
            errors.append(str(exc))
    crossref = None
    doi = normalize_doi(str(book.meta.get("doi") or ""))
    if doi:
        try:
            crossref = lookup_doi(doi)
        except BookLookupError as exc:
            errors.append(str(exc))
    authors = (
        book.authors or (found.authors if found else []) or (crossref.authors if crossref else [])
    )
    work, work_error = _work_facts(book.title, authors, str(book.meta.get("original_title") or ""))
    report.update(filled=[], work=work.match if work else "none")
    if work_error:
        errors.append(work_error)
    if found is None and work is None and crossref is None:
        raise BookEditError(
            " ".join(errors)
            or "Nem os catálogos, nem o Crossref, nem o Wikidata acharam esta ficha."
        )
    fields: dict[str, Any] = {}
    if found:
        fields = {**found.form_fields(), "authors": found.authors}
        fields.pop("original_publication_year", None)  # o ano da obra vem do Wikidata
    if crossref and crossref.authors and not fields.get("authors"):
        fields["authors"] = crossref.authors
    if crossref and crossref.pages and not fields.get("pages"):
        fields["pages"] = crossref.pages
    if work:
        fields.update(work.book_fields())
    cover = _search_cover(cfg, found.cover_ref) if found else None
    source: dict[str, Any] = (
        {"publisher": found.publisher, "place": found.place, "year": found.year} if found else {}
    )
    if crossref:
        source.update(
            {
                "publisher": source.get("publisher") or crossref.publisher,
                "year": source.get("year") or crossref.year,
                "journal": crossref.container,
                "volume": crossref.volume,
                "issue": crossref.issue,
                "pages": crossref.pages,
            }
        )
        if crossref.document_type and not str(book.meta.get("document_type") or "").strip():
            source["document_type"] = crossref.document_type

    def change(book, db):
        filled = fill_missing(book, fields)
        if cover and not book.meta.get("cover"):
            set_cover(book, store_cover(cfg.vault_path, *cover))
            filled.append("cover")
        if any(value not in (None, "") for value in source.values()):
            filled += update_source(cfg, db, book, source, only_missing=True)
        report["filled"] = filled

    return change


_NOTE_FIELDS = (
    "topic",
    "page",
    "excerpt",
    "summary",
    "concepts",
    "candidate",
    "reflection_kind",
    "characters",
    "reflection",
    "analogy",
)


def _note_draft(form: FormData) -> dict[str, str]:
    return {key: str(form.get(key) or "") for key in _NOTE_FIELDS}


def _notice_from_match(match: Any, *, mode: str, chunk_id: str = "", use_llm: bool = False) -> dict:
    if match.needs_excerpt:
        kind = "excerpt"
    elif match.unchecked:
        kind = "unchecked"
    else:
        kind = "hits"
    return {
        "kind": kind,
        "mode": mode,
        "chunk_id": chunk_id,
        "use_llm": use_llm,
        "hits": [
            {
                "title": item.title,
                "edition": item.edition,
                "href": item.href,
                "probable": item.probable,
            }
            for item in match.passages
        ],
    }


def _edition_gate(cfg: Any, source_id: str, excerpt: str, *, confirm: bool):
    """``None`` quando dá para gravar. Senão, o aviso para a página.

    Sem obra, grava direto. Sem trecho, pede o trecho e não consulta o título.
    Com confirmação e trecho, grava. O primeiro POST com vizinhas não grava.
    """
    from zettel.library.edition_match import compare_excerpt
    from zettel.library.works import sibling_source_ids

    db = service_db(cfg)
    try:
        linked = bool(sibling_source_ids(db, source_id))
        if not linked:
            return None
        if not (excerpt or "").strip():
            from zettel.library.edition_match import EditionMatch

            return _notice_from_match(EditionMatch(needs_excerpt=True), mode="lit")
        if confirm:
            return None
        match = compare_excerpt(cfg, db, source_id, excerpt)
    finally:
        db.close()
    if match.clear:
        return None
    return _notice_from_match(match, mode="lit")


def service_db(cfg: Any):
    """Abre o banco já espelhado. Quem chama fecha."""
    from zettel.library.mirror import refresh
    from zettel.state import StateDB

    db = StateDB(cfg.state_db_path)
    refresh(db, cfg)
    return db


@router.post("/books/{source_id}/reading-notes")
def book_reading_note(request: Request, source_id: str, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.library.reading_notes import ReadingNote, ReadingNoteError, create_reading_note

    try:
        note = ReadingNote.from_form(form)
    except ReadingNoteError as exc:
        return error_page(request, 400, str(exc))
    cfg = service(request).cfg
    confirm = str(form.get("confirm") or "") == "1"
    notice = _edition_gate(cfg, source_id, note.excerpt, confirm=confirm)
    if notice is not None:
        notice["mode"] = "lit"
        return _book_page(request, source_id, draft=_note_draft(form), edition_notice=notice)

    def change(book, db):
        create_reading_note(cfg, db, book, note)
        return "saved"

    # Só anexa uma nota e, no máximo, liga a fonte: não depende da versão da página.
    return _edit(request, source_id, None, change, "notas")


@router.post("/books/{source_id}/permanent")
def book_permanent(request: Request, source_id: str, form: FormData = Depends(read_form)):
    """ZTL a partir de uma nota de leitura: o job ``manual-ztl-from-lit`` de sempre.

    Com uma obra vinculada, o trecho já guardado na LIT é comparado com a outra
    edição antes de enfileirar. A confirmação repete o envio e gera a nota.
    """
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    chunk_id = str(form.get("chunk_id") or "").strip()
    use_llm = str(form.get("use_llm") or "") == "1"
    confirm = str(form.get("confirm") or "") == "1"
    if not chunk_id.startswith("@"):
        return error_page(request, 400, "Escolha a nota de leitura.")
    if use_llm:
        from zettel.credentials import llm_phase_ready

        if not llm_phase_ready(service(request).cfg, "connect"):
            return error_page(request, 409, "A fase connect não tem credencial de LLM.")
    cfg = service(request).cfg
    db = service_db(cfg)
    try:
        chunk = db.get_chunk(chunk_id) or {}
        excerpt = str(chunk.get("text") or "")
        from zettel.library.edition_match import EditionMatch, compare_excerpt
        from zettel.library.works import sibling_source_ids

        linked = bool(sibling_source_ids(db, source_id))
        notice = None
        if linked and not confirm:
            if not excerpt.strip():
                notice = _notice_from_match(EditionMatch(needs_excerpt=True), mode="ztl")
            else:
                match = compare_excerpt(cfg, db, source_id, excerpt)
                if not match.clear:
                    notice = _notice_from_match(
                        match, mode="ztl", chunk_id=chunk_id, use_llm=use_llm
                    )
    finally:
        db.close()
    if notice is not None:
        notice["mode"] = "ztl"
        notice["chunk_id"] = chunk_id
        notice["use_llm"] = use_llm
        return _book_page(request, source_id, edition_notice=notice)
    payload = {"ref": chunk_id, "chunk_id": chunk_id, "thesis": None, "use_llm": use_llm}
    return post_job(request, "manual-ztl-from-lit", payload, str(form.get("csrf") or ""))
