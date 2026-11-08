# syntax=docker/dockerfile:1
# Web UI e CLI do Zettel. Veja docs/instalacao.md#docker.
#
#   docker compose up -d                                    # web em http://localhost:5000
#   docker compose run --rm zettel python -m zettel doctor  # CLI
#
# TORCH_EXTRA=cpu (padrao) ou cu126 (precisa do NVIDIA Container Toolkit no host).

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ARG TORCH_EXTRA=cpu
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change does not reinstall torch and Docling.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project --extra "${TORCH_EXTRA}"
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra "${TORCH_EXTRA}"

FROM python:${PYTHON_VERSION}-slim
RUN useradd --create-home --uid 1000 zettel
WORKDIR /app
COPY --from=build --chown=zettel:zettel /app /app
# HF_HOME: Docling downloads its layout/table models there on the first PDF harvest;
# docker-compose.yml keeps it in a volume so a new container does not fetch them again.
ENV PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/home/zettel/.cache/huggingface \
    PYTHONUNBUFFERED=1
USER zettel
EXPOSE 5000
CMD ["uvicorn", "zettel.web:app", "--host", "0.0.0.0", "--port", "5000"]
