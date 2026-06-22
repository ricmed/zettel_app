# Zettelkasten — Automated Note Generation Pipeline

A Python CLI tool that reads PDF and Markdown files and automatically generates Zettelkasten-style notes (Source, Literature, and Permanent notes) compatible with Obsidian. Uses LLMs (OpenAI by default) for concept extraction and note generation, ChromaDB for semantic search, and SQLite for pipeline state.

## How to Run

```bash
# Initialize vault and database
python -m zettel init

# Full pipeline (harvest → extract → connect → garden)
python -m zettel run-all

# Individual phases
python -m zettel harvest    # Scan inbox, extract text, create SRC/LIT notes
python -m zettel extract    # Extract concepts via LLM
python -m zettel connect    # Generate permanent notes
python -m zettel garden     # Cluster notes, generate MOCs

# Utilities
python -m zettel status     # Show pipeline statistics
python -m zettel doctor     # Check config and dependencies
```

## Setup

1. Set the `OPENAI_API_KEY` environment variable (required for LLM features)
2. Drop PDF or Markdown files in `data/inbox/`
3. Run `python -m zettel run-all`
4. Open the `vault/` folder in Obsidian

## Configuration

Edit `config/config.yaml` to adjust:
- LLM provider and model (OpenAI, Anthropic, Ollama)
- Embedding model
- Chunk size and overlap
- Deduplication thresholds
- MOC clustering settings

## Project Structure

- `zettel/` — Main Python package (CLI, pipeline phases)
- `config/config.yaml` — Main configuration
- `prompts/` — LLM prompt templates
- `data/inbox/` — Drop zone for input files
- `data/chroma/` — ChromaDB vector store
- `data/state.db` — SQLite pipeline state
- `vault/` — Generated Obsidian vault

## User Preferences

- Language: Portuguese (pt-BR) for generated notes
- LLM: OpenAI gpt-4o-mini by default
