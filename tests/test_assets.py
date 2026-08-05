"""Tests for image extraction, dedup, and description caching — Fase 3."""

import pytest

from zettel.assets import (
    asset_id_for,
    asset_ids_in_text,
    describe_pending_assets,
    extract_markdown_images,
    register_assets,
    reresolve_asset_chapters,
)
from zettel.config import AppConfig
from zettel.state import StateDB


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


def _cfg(tmp_path, **images):
    cfg = AppConfig(vault_path=tmp_path / "vault")
    cfg.images.enabled = True
    for k, v in images.items():
        setattr(cfg.images, k, v)
    return cfg


# Arbitrary bytes standing in for image content. The Markdown/description paths only
# copy + hash + base64 the bytes (no decoding), so a real PNG isn't needed here.
_PNG = b"\x89PNG\r\n\x1a\n-conteudo-de-imagem-para-teste-"


def test_markdown_local_image_extracted_and_rewritten(tmp_path):
    cfg = _cfg(tmp_path)
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    (src_dir / "fig.png").write_bytes(_PNG)
    src_file = src_dir / "doc.md"
    body = "Antes.\n\n![legenda](fig.png)\n\nDepois."

    new_body, images = extract_markdown_images(cfg, body, src_file)
    assert len(images) == 1
    # Reference rewritten to a 90_Assets path.
    assert "90_Assets/" in new_body
    # File copied into the vault.
    saved = cfg.vault_path / images[0]["path"]
    assert saved.exists()
    assert "Antes" in images[0]["context_snippet"]


def test_markdown_remote_image_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    src_file = tmp_path / "doc.md"
    body = "![x](https://example.com/a.png)"
    new_body, images = extract_markdown_images(cfg, body, src_file)
    assert images == []
    assert new_body == body


def test_disabled_images_noop(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.images.enabled = False
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    (src_dir / "fig.png").write_bytes(_PNG)
    body = "![x](fig.png)"
    new_body, images = extract_markdown_images(cfg, body, src_dir / "doc.md")
    assert images == []
    assert new_body == body


def test_identical_images_dedup_to_same_file(tmp_path):
    cfg = _cfg(tmp_path)
    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    (src_dir / "a.png").write_bytes(_PNG)
    (src_dir / "b.png").write_bytes(_PNG)  # identical content
    body = "![one](a.png)\n\n![two](b.png)"
    _, images = extract_markdown_images(cfg, body, src_dir / "doc.md")
    # Same content -> same content-addressed path.
    assert images[0]["path"] == images[1]["path"]


def test_register_assets_resolves_chapter(tmp_path, db):
    cfg = _cfg(tmp_path)
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    images = [{"checksum": "ck1", "path": "90_Assets/img-abc.png", "context_snippet": "ctx"}]
    chapters = [
        {"title": "Cap1", "text": "sem imagem aqui", "locator": "Cap1"},
        {"title": "Cap2", "text": "veja 90_Assets/img-abc.png no texto", "locator": "Cap2"},
    ]
    register_assets(db, "@S", chapters, images)
    asset = db.get_asset(asset_id_for("@S", "ck1"))
    assert asset is not None
    assert asset["chapter_id"] == "@S::ch001"  # resolved to chapter 2


def test_reresolve_asset_chapters_updates_orphan_ids(tmp_path, db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_asset(
        "@S::img::x", "@S", "90_Assets/img-x.png", "ckx",
        chapter_id="@S::ch026",  # orphan from interrupted harvest
    )
    chapters = [
        {"title": "Early", "text": "sem imagem", "locator": "Early"},
        {"title": "Late", "text": "diagrama 90_Assets/img-x.png aqui", "locator": "Late"},
    ]
    n = reresolve_asset_chapters(db, "@S", chapters)
    assert n == 1
    assert db.get_asset("@S::img::x")["chapter_id"] == "@S::ch001"


def test_asset_ids_in_text_matches_paths(tmp_path, db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_asset("@S::img::a", "@S", "90_Assets/img-aaa.png", "cka")
    db.upsert_asset("@S::img::b", "@S", "90_Assets/img-bbb.png", "ckb")
    text = "Antes ![Imagem](90_Assets/img-aaa.png) depois"
    assert asset_ids_in_text(db, "@S", text) == ["@S::img::a"]
    assert asset_ids_in_text(db, "@S", "sem figuras") == []


def test_describe_pending_assets_uses_cache(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path)
    # Create a source + a saved image + a pending asset.
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    img_rel = "90_Assets/img-xyz.png"
    (cfg.vault_path / img_rel).parent.mkdir(parents=True, exist_ok=True)
    (cfg.vault_path / img_rel).write_bytes(_PNG)
    db.upsert_asset("@S::img::a", "@S", img_rel, "ckimg", context_snippet="um grafico")

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages):
            calls["n"] += 1
            class R:
                content = "Um grafico de barras comparando modelos."
            return R()

    monkeypatch.setattr("zettel.assets._get_multimodal_llm", lambda cfg, model: FakeLLM())

    n1 = describe_pending_assets(cfg, db)
    assert n1 == 1
    assert calls["n"] == 1
    asset = db.get_asset("@S::img::a")
    assert asset["status"] == "described"
    assert "grafico" in asset["description"]

    # Reset to pending and re-run: cache hit => no new LLM call.
    db.reset_failed_assets()  # no-op here, but ensure method exists
    db.upsert_asset("@S::img::a", "@S", img_rel, "ckimg", context_snippet="um grafico")
    # upsert keeps status? upsert_asset ON CONFLICT does not reset status, so still described.
    # Force pending to test cache path:
    db.conn.execute("UPDATE assets SET status='pending' WHERE asset_id='@S::img::a'")
    db.conn.commit()
    n2 = describe_pending_assets(cfg, db)
    assert n2 == 1
    assert calls["n"] == 1  # served from cache, no extra call


def _pending_asset(tmp_path, db, cfg, asset_id="@S::img::a", rel="90_Assets/img-xyz.png"):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    (cfg.vault_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (cfg.vault_path / rel).write_bytes(_PNG)
    db.upsert_asset(asset_id, "@S", rel, "ckimg", context_snippet="um grafico")
    return asset_id


def test_describe_retries_rate_limit_then_succeeds(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path, min_interval_seconds=0, rate_limit_max_retries=3)
    asset_id = _pending_asset(tmp_path, db, cfg)
    calls = {"n": 0}
    sleeps: list[float] = []

    class FakeLLM:
        def invoke(self, messages):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError(
                    "Error code: 429 - Rate limit reached for gpt-4o-mini "
                    "on tokens per min (TPM): Limit 200000. Please try again in 392ms."
                )

            class R:
                content = "Diagrama de entidades apos retry."

            return R()

    monkeypatch.setattr("zettel.assets._get_multimodal_llm", lambda cfg, model: FakeLLM())
    monkeypatch.setattr("zettel.assets.time.sleep", lambda s: sleeps.append(s))

    n = describe_pending_assets(cfg, db)
    assert n == 1
    assert calls["n"] == 3
    assert db.get_asset(asset_id)["status"] == "described"
    assert sleeps  # waited on 429
    assert sleeps[0] >= 0.5  # floor over the 392ms hint


def test_describe_rate_limit_exhausted_keeps_pending(tmp_path, db, monkeypatch):
    cfg = _cfg(
        tmp_path,
        min_interval_seconds=0,
        rate_limit_max_retries=1,
        rate_limit_abort_after=1,
        rate_limit_backoff_max=1.0,
    )
    asset_id = _pending_asset(tmp_path, db, cfg)
    sleeps: list[float] = []

    class Always429:
        def invoke(self, messages):
            raise RuntimeError(
                "Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}"
            )

    monkeypatch.setattr("zettel.assets._get_multimodal_llm", lambda cfg, model: Always429())
    monkeypatch.setattr("zettel.assets.time.sleep", lambda s: sleeps.append(s))

    n = describe_pending_assets(cfg, db)
    assert n == 0
    assert db.get_asset(asset_id)["status"] == "pending"  # never failed


def test_describe_non_rate_limit_error_marks_failed(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path, min_interval_seconds=0)
    asset_id = _pending_asset(tmp_path, db, cfg)

    class Boom:
        def invoke(self, messages):
            raise RuntimeError("connection reset by peer")

    monkeypatch.setattr("zettel.assets._get_multimodal_llm", lambda cfg, model: Boom())
    monkeypatch.setattr("zettel.assets.time.sleep", lambda s: None)

    n = describe_pending_assets(cfg, db)
    assert n == 0
    assert db.get_asset(asset_id)["status"] == "failed"


def test_describe_aborts_batch_after_consecutive_rate_limits(tmp_path, db, monkeypatch):
    cfg = _cfg(
        tmp_path,
        min_interval_seconds=0,
        rate_limit_max_retries=0,
        rate_limit_abort_after=2,
        rate_limit_backoff_max=1.0,
    )
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    for i, aid in enumerate(("@S::img::a", "@S::img::b", "@S::img::c")):
        rel = f"90_Assets/img-{i}.png"
        (cfg.vault_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg.vault_path / rel).write_bytes(_PNG + bytes([i]))
        db.upsert_asset(aid, "@S", rel, f"ck{i}", context_snippet="ctx")

    class Always429:
        def invoke(self, messages):
            raise RuntimeError("Error code: 429 - rate_limit_exceeded")

    monkeypatch.setattr("zettel.assets._get_multimodal_llm", lambda cfg, model: Always429())
    monkeypatch.setattr("zettel.assets.time.sleep", lambda s: None)

    n = describe_pending_assets(cfg, db)
    assert n == 0
    statuses = {
        a["asset_id"]: a["status"]
        for a in (db.get_asset("@S::img::a"), db.get_asset("@S::img::b"), db.get_asset("@S::img::c"))
    }
    assert statuses == {
        "@S::img::a": "pending",
        "@S::img::b": "pending",
        "@S::img::c": "pending",  # never attempted after abort
    }


def test_parse_retry_after_seconds():
    from zettel.assets import _parse_retry_after_seconds

    assert _parse_retry_after_seconds(RuntimeError("try again in 392ms")) == pytest.approx(0.392)
    assert _parse_retry_after_seconds(RuntimeError("try again in 1.5s")) == pytest.approx(1.5)
    assert _parse_retry_after_seconds(RuntimeError("no hint")) is None
