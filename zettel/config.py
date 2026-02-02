"""Configuration loader and validator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = 0
    top_p: float = 1
    max_retries: int = 2


class EmbeddingConfig(BaseModel):
    provider: str = "openai"
    model: str = "text-embedding-3-small"


class ChunkingConfig(BaseModel):
    chunk_size: int = 1000
    chunk_overlap: int = 200


class LinkingConfig(BaseModel):
    topk: int = 5
    dedupe_threshold: float = 0.85


class GardenerConfig(BaseModel):
    min_cluster_size: int = 5
    min_notes_for_moc: int = 3


class AppConfig(BaseModel):
    vault_path: Path = Path("./vault")
    inbox_path: Path = Path("./data/inbox")
    chroma_path: Path = Path("./data/chroma")
    state_db_path: Path = Path("./data/state.db")
    cache_path: Path = Path("./data/cache")
    prompts_path: Path = Path("./prompts")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    linking: LinkingConfig = Field(default_factory=LinkingConfig)
    gardener: GardenerConfig = Field(default_factory=GardenerConfig)

    language: str = "pt-BR"
    log_level: str = "INFO"
    pdf_extractor: str = "docling"
    device: str = "auto"  # auto | cpu | cuda

    @field_validator(
        "vault_path",
        "inbox_path",
        "chroma_path",
        "state_db_path",
        "cache_path",
        "prompts_path",
        mode="before",
    )
    @classmethod
    def resolve_path(cls, v: Any) -> Path:
        return Path(v).resolve()


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load configuration from a YAML file, with defaults.

    Also loads environment variables from a .env file (project root).
    """
    # Load .env before anything that reads env vars (LLM keys, etc.)
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.info("Variaveis de ambiente carregadas de .env")
    else:
        logger.debug(".env nao encontrado, usando apenas variaveis de ambiente do sistema")

    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                data = raw
        logger.info("Configuração carregada de %s", config_path)
    else:
        logger.warning("Arquivo de config não encontrado: %s — usando defaults", config_path)

    return AppConfig(**data)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with RichHandler.

    Uses stderr so log messages flow correctly alongside Rich console.status() spinners.
    """
    from rich.logging import RichHandler
    from rich.console import Console

    handler = RichHandler(
        console=Console(stderr=True),
        show_time=True,
        show_path=False,
        markup=False,
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[handler],
    )


def detect_device(preference: str = "auto") -> str:
    """Detect the best available compute device.

    Args:
        preference: "auto" (detect), "cpu" (force CPU), "cuda" (force GPU).

    Returns:
        "cuda" if a GPU is available and selected, otherwise "cpu".
    """
    if preference == "cpu":
        logger.info("Dispositivo: CPU (forcado via config)")
        return "cpu"

    if preference == "cuda":
        if _cuda_available():
            logger.info("Dispositivo: CUDA (forcado via config) — %s", _gpu_name())
            return "cuda"
        logger.warning("CUDA solicitado mas nao disponivel. Usando CPU.")
        return "cpu"

    # auto
    if _cuda_available():
        logger.info("GPU detectada: %s — usando CUDA", _gpu_name())
        return "cuda"

    logger.info("Nenhuma GPU detectada. Usando CPU.")
    return "cpu"


def _cuda_available() -> bool:
    """Check if CUDA-capable GPU is available via PyTorch."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _gpu_name() -> str:
    """Return the name of the current CUDA device."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return "desconhecida"


def get_gpu_info() -> dict[str, Any]:
    """Return detailed GPU information for diagnostics."""
    info: dict[str, Any] = {"available": False}
    try:
        import torch
        info["torch_version"] = torch.__version__
        info["cuda_built"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            info["available"] = True
            info["device_name"] = torch.cuda.get_device_name(0)
            info["device_count"] = torch.cuda.device_count()
            mem = torch.cuda.get_device_properties(0).total_mem
            info["vram_gb"] = round(mem / (1024 ** 3), 1)
            info["cuda_version"] = torch.version.cuda or "N/A"
    except ImportError:
        info["torch_version"] = "nao instalado"
    return info
