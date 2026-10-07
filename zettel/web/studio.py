"""Acervo: ask, catalog, summarize, article and skill (ADR-056)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import post_job
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

_MODES = {"hybrid", "vector"}
_STYLES = {"blog", "academic"}


class StudioFormError(Exception):
    def __init__(self, message: str):
        self.message = message


def _optional_int(raw: str, *, lo: int, hi: int, label: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise StudioFormError(f"{label} deve ser um número.") from exc
    if value < lo or value > hi:
        raise StudioFormError(f"{label} deve estar entre {lo} e {hi}.")
    return value


def _mode(raw: str) -> str | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text not in _MODES:
        raise StudioFormError("Modo deve ser hybrid ou vector.")
    return text


def _slug(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    from zettel.vault import _slug as slugify

    cleaned = slugify(text, 60)
    if not cleaned:
        raise StudioFormError("O slug não produz um nome de pasta válido.")
    return cleaned


def _categories(cfg) -> list[str]:
    from zettel.gardener_assign import category_pairs

    try:
        pairs = category_pairs(cfg.gardener)
    except (OSError, ValueError):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for _pillar, name in pairs:
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _personalities(cfg) -> list[dict[str, str]]:
    from zettel.article import load_personalities

    profiles = load_personalities(cfg.retrieval.article.personalities_path)
    return [
        {"id": key, "name": str(profile.get("name") or key)} for key, profile in profiles.items()
    ]


def _page(request: Request, *, status_code: int = 200, error: str | None = None, **extra):
    from zettel.preflight import estimate_article, estimate_summarize
    from zettel.web.health import embedding_ready, llm_phase_ready

    cfg = service(request).cfg
    db = service(request).db()
    try:
        sources = [
            {
                "source_id": row["source_id"],
                "title": row.get("title") or row.get("citekey") or row["source_id"],
            }
            for row in db.list_sources_with_authors()
        ]
        mocs = [
            {"moc_id": row["moc_id"], "topic": row.get("topic") or row["moc_id"]}
            for row in db.list_mocs()
        ]
        summarize_estimate = estimate_summarize(cfg, db, None)
        index_busy = db.has_active_web_job()
    finally:
        db.close()
    return render(
        request,
        "studio.html",
        page="studio",
        status_code=status_code,
        error=error,
        sources=sources,
        mocs=mocs,
        categories=_categories(cfg),
        personalities=_personalities(cfg),
        ask_ready=llm_phase_ready(cfg, "ask"),
        article_ready=llm_phase_ready(cfg, "article"),
        summarize_ready=llm_phase_ready(cfg, "summarize"),
        embedding_ready=embedding_ready(cfg),
        summarize_estimate=summarize_estimate,
        article_estimate=estimate_article(cfg),
        index_busy=index_busy,
        **extra,
    )


def _cell(value: object) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def catalog_copy_text(result, *, show_context: bool) -> str:
    """Markdown of the catalog the page is showing, for the copy button."""
    lines = [f"# Catálogo: {result.query}", ""]
    if not result.sources:
        lines.append("Nenhuma fonte do acervo trata desse assunto com evidência suficiente.")
    for src in result.sources:
        lines.append(f"## {src.title}")
        lines.append(f"{src.citekey} · {src.total_notes} nota(s) permanente(s)")
        lines.append("")
        lines.append("| Capítulo | Páginas | Notas | Achado por |")
        lines.append("| --- | --- | ---: | --- |")
        for chapter in src.chapters:
            title = chapter.chapter_title + (" [defasado]" if chapter.summary_stale else "")
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(title),
                        _cell(chapter.page_label or "-"),
                        str(chapter.note_count),
                        _cell(chapter.origin_label),
                    ]
                )
                + " |"
            )
        lines.append("")
    if show_context and result.retrieval_params:
        lines.append("## Parâmetros")
        lines.append("")
        for key, value in result.retrieval_params.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    if show_context and result.candidates:
        lines.append("## Capítulos avaliados")
        lines.append("")
        lines.append("| Capítulo | Fonte | Sim. | Passou | Motivo |")
        lines.append("| --- | --- | --- | --- | --- |")
        for chapter in result.candidates:
            similarity = (
                f"{chapter.summary_similarity:.2f}"
                if chapter.summary_similarity is not None
                else "-"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(chapter.chapter_title),
                        _cell(chapter.source_id),
                        similarity,
                        "sim" if chapter.passed_floor else "não",
                        _cell(chapter.floor_reason or "-"),
                    ]
                )
                + " |"
            )
    return "\n".join(lines).strip() + "\n"


def _catalog(request: Request, subject: str, show_context: bool):
    from zettel.catalog import run_catalog
    from zettel.index import VectorIndex, index_kwargs
    from zettel.web.health import embedding_ready

    cfg = service(request).cfg
    if not embedding_ready(cfg):
        return _page(
            request,
            status_code=409,
            error="A credencial de embedding não está configurada.",
            catalog_subject=subject,
            catalog_show_context=show_context,
        )
    db = service(request).db()
    try:
        if db.has_active_web_job():
            return _page(
                request,
                status_code=409,
                error="O índice está em uso por outra execução. Tente de novo quando ela terminar.",
                catalog_subject=subject,
                catalog_show_context=show_context,
            )
        idx = VectorIndex(**index_kwargs(cfg))
        result = run_catalog(cfg, db, idx, subject)
    finally:
        db.close()
    return _page(
        request,
        catalog=result,
        catalog_subject=subject,
        catalog_show_context=show_context,
        catalog_copy=catalog_copy_text(result, show_context=show_context),
    )


@router.get("/studio", response_class=HTMLResponse)
async def studio(request: Request, subject: str = "", show_context: str = ""):
    if not authenticated(request):
        return redirect_login()
    text = subject.strip()
    if not text:
        return _page(request, catalog_show_context=True)
    if len(text) > 500:
        return _page(
            request,
            status_code=400,
            error="O assunto deve ter no máximo 500 caracteres.",
            catalog_subject=text[:500],
        )
    show = show_context in {"1", "on", "true"}
    return _catalog(request, text, show)


@router.post("/studio/ask")
async def studio_ask(
    request: Request,
    csrf: str = Form(""),
    question: str = Form(""),
    topk: str = Form(""),
    mode: str = Form(""),
    no_graph: str = Form(""),
    show_context: str = Form(""),
    save: str = Form(""),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    try:
        text = question.strip()
        if not text:
            raise StudioFormError("Escreva a pergunta.")
        if len(text) > 2000:
            raise StudioFormError("A pergunta deve ter no máximo 2000 caracteres.")
        payload = {
            "question": text,
            "topk": _optional_int(topk, lo=1, hi=100, label="Top-k"),
            "mode": _mode(mode),
            "no_graph": no_graph == "1",
            "show_context": show_context == "1",
            "save": save == "1",
        }
    except StudioFormError as exc:
        return _page(request, status_code=400, error=exc.message)
    from zettel.web.health import llm_phase_ready

    if not llm_phase_ready(service(request).cfg, "ask"):
        return _page(
            request,
            status_code=409,
            error="O provedor da fase ask não possui credencial configurada.",
        )
    return post_job(request, "ask", payload, csrf)


@router.post("/studio/summarize")
async def studio_summarize(
    request: Request,
    csrf: str = Form(""),
    source_id: str = Form(""),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    from zettel.web.health import llm_phase_ready

    if not llm_phase_ready(service(request).cfg, "summarize"):
        return _page(
            request,
            status_code=409,
            error="O provedor da fase summarize não possui credencial configurada.",
        )
    return post_job(request, "summarize", {"source_id": source_id.strip() or None}, csrf)


@router.post("/studio/skill")
async def studio_skill(
    request: Request,
    csrf: str = Form(""),
    source_id: str = Form(""),
    moc_id: str = Form(""),
    topic: str = Form(""),
    slug: str = Form(""),
    overwrite: str = Form(""),
    include_excerpts: str = Form(""),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    try:
        chosen = [value.strip() for value in (source_id, moc_id, topic) if value.strip()]
        if len(chosen) != 1:
            raise StudioFormError("Escolha exatamente um recorte: fonte, MOC ou categoria.")
        payload = {
            "source_id": source_id.strip() or None,
            "moc_id": moc_id.strip() or None,
            "topic": topic.strip() or None,
            "slug": _slug(slug),
            "overwrite": overwrite == "1",
            "include_excerpts": include_excerpts == "1",
        }
    except StudioFormError as exc:
        return _page(request, status_code=400, error=exc.message)
    return post_job(request, "skill", payload, csrf)


@router.post("/studio/article")
async def studio_article(
    request: Request,
    csrf: str = Form(""),
    topic: str = Form(""),
    style: str = Form("blog"),
    personality: str = Form(""),
    style_notes: str = Form(""),
    topk: str = Form(""),
    mode: str = Form(""),
    no_graph: str = Form(""),
    outline_only: str = Form(""),
    review_context: str = Form(""),
    review_outline: str = Form(""),
    skip_judge: str = Form(""),
    max_judge_iterations: str = Form(""),
    save: str = Form(""),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    try:
        text = topic.strip()
        if not text:
            raise StudioFormError("Escreva o tema do artigo.")
        if len(text) > 500:
            raise StudioFormError("O tema deve ter no máximo 500 caracteres.")
        style_norm = (style or "blog").strip().lower()
        if style_norm not in _STYLES:
            raise StudioFormError("O estilo deve ser blog ou academic.")
        notes = style_notes.strip()
        if len(notes) > 2000:
            raise StudioFormError("As notas de estilo devem ter no máximo 2000 caracteres.")
        personality_id = personality.strip() or None
        if personality_id is not None:
            known = {item["id"] for item in _personalities(service(request).cfg)}
            if personality_id not in known:
                raise StudioFormError("Personalidade desconhecida.")
        payload = {
            "topic": text,
            "style": style_norm,
            "personality": personality_id,
            "style_notes": notes or None,
            "topk": _optional_int(topk, lo=1, hi=100, label="Top-k"),
            "mode": _mode(mode),
            "no_graph": no_graph == "1",
            "outline_only": outline_only == "1",
            "review_context": review_context == "1",
            "review_outline": review_outline == "1",
            "skip_judge": skip_judge == "1",
            "max_judge_iterations": _optional_int(
                max_judge_iterations, lo=1, hi=20, label="Ciclos do juiz"
            ),
            "save": save == "1",
        }
    except StudioFormError as exc:
        return _page(request, status_code=400, error=exc.message)
    from zettel.web.health import llm_phase_ready

    if not llm_phase_ready(service(request).cfg, "article"):
        return _page(
            request,
            status_code=409,
            error="O provedor da fase article não possui credencial configurada.",
        )
    return post_job(request, "article", payload, csrf)
