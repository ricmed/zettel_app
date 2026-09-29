"""Disk-backed extraction snapshot for web bibliography review.

Only the review id (a server-generated UUID) is used to address these files.
The snapshot is never exposed through an HTTP route.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from zettel.config import AppConfig


def snapshot_path(cfg: AppConfig, review_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", review_id):
        raise ValueError("Revisão inválida.")
    return cfg.cache_path / "harvest-reviews" / f"{review_id}.json.gz"


def asset_stage_path(cfg: AppConfig, review_id: str) -> Path:
    snapshot_path(cfg, review_id)  # validates server-generated review id
    return cfg.cache_path / "harvest-reviews" / f"{review_id}-assets"


def remove_preparation(cfg: AppConfig, review_id: str) -> None:
    snapshot_path(cfg, review_id).unlink(missing_ok=True)
    shutil.rmtree(asset_stage_path(cfg, review_id), ignore_errors=True)


def discard_orphan_preparations(cfg: AppConfig, db: Any) -> None:
    """Reconcile snapshots after a crash between disk, review, and job commits."""
    directory = cfg.cache_path / "harvest-reviews"
    if not directory.is_dir():
        return
    for snapshot in directory.glob("*.json.gz"):
        review_id = snapshot.name.removesuffix(".json.gz")
        if not re.fullmatch(r"[a-f0-9]{32}", review_id):
            continue
        job = db.get_web_job(review_id)
        review = (
            db.get_web_harvest_review(review_id, job["payload"].get("session_hash", ""))
            if job and job["operation"] == "prepare_harvest"
            else None
        )
        confirmed = (
            db.get_web_job(review["harvest_job_id"])
            if review and review["harvest_job_id"]
            else None
        )
        if (
            not job or job["state"] != "succeeded" or not review
            or review["state"] == "cancelled"
            or (confirmed and confirmed["state"] == "succeeded")
        ):
            remove_preparation(cfg, review_id)
    for staged in directory.glob("*-assets"):
        review_id = staged.name.removesuffix("-assets")
        if re.fullmatch(r"[a-f0-9]{32}", review_id) and not snapshot_path(
            cfg, review_id
        ).exists():
            shutil.rmtree(staged, ignore_errors=True)


def promote_staged_assets(cfg: AppConfig, review_id: str) -> None:
    """Install content-addressed assets only after confirmed deduplication."""
    stage = asset_stage_path(cfg, review_id) / "90_Assets"
    if not stage.exists():
        return
    target = cfg.vault_path / "90_Assets"
    target.mkdir(parents=True, exist_ok=True)
    for image in stage.iterdir():
        if not image.is_file() or not re.fullmatch(r"img-[a-f0-9]{16}\.[a-zA-Z0-9]+", image.name):
            raise ValueError("Imagem preparada inválida. Prepare o documento novamente.")
        destination = target / image.name
        if not destination.exists():
            shutil.copyfile(image, destination)


def save_snapshot(
    cfg: AppConfig,
    review_id: str,
    *,
    checksum: str,
    text: str,
    metadata: dict[str, Any],
) -> None:
    path = snapshot_path(cfg, review_id)
    from zettel.paging import compute_docling_config_hash

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{review_id}-{uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output, gzip.GzipFile(
            fileobj=output, mode="wb"
        ) as compressed:
            compressed.write(
                json.dumps(
                    {
                        "checksum": checksum,
                        "config_hash": compute_docling_config_hash(cfg),
                        "text": text,
                        "metadata": metadata,
                    },
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_snapshot(cfg: AppConfig, review_id: str) -> dict[str, Any]:
    try:
        with gzip.open(snapshot_path(cfg, review_id), "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "A preparação não está mais disponível. Prepare o documento novamente."
        ) from exc
