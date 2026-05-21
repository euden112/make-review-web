# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A capstone project that aggregates and AI-summarizes game reviews from Steam and Metacritic. The pipeline collects reviews via crawlers, stores them in PostgreSQL, runs a Map-Reduce AI pipeline (local Ollama for map stage, Groq API for reduce stage) to generate summaries, and serves them through a FastAPI backend with a React frontend.

## Services

All services are orchestrated via Docker Compose:

```bash
docker-compose up --build        # Start all services
docker-compose up postgres redis # Start only infrastructure
docker-compose down -v           # Teardown with volume cleanup
```

Services:
- **postgres** (port 5432) — PostgreSQL 15 with schema auto-applied from `database/` on first boot
- **redis** (port 6379) — Cache for AI summaries
- **ollama** (port 11434) — Local LLM for map stage (`gemma3:4b` by default)
- **backend** (port 8000) — FastAPI, auto-reloads from `./backend`
- **frontend** (port 80) — React/Vite served via nginx
- **adminer** (port 8888) — Database UI

## Environment Variables

Copy `.env.example` to `.env` and fill in:
```
GROQ_API_KEY=...
GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct   # default
```

The backend also reads `OLLAMA_BASE_URL` and `LOCAL_MAP_MODEL` from the environment (set in `docker-compose.yml`).

## Development Commands

### Backend (FastAPI)

```bash
# Local dev without Docker (from repo root)
pip install -r requirements.txt -r backend/requirements.txt
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# API docs
open http://localhost:8000/docs
```

### Frontend (React + Vite)

```bash
cd frontend
npm install
npm run dev      # Dev server at http://localhost:5173 (proxies /api → localhost:8000)
npm run build    # Production build
npm run lint     # ESLint
```

### Crawlers

```bash
# Collect Steam reviews (writes to crawling/steam/reviews_steam.json)
python crawling/steam/steam_crawler.py

# Collect Metacritic reviews
python crawling/metacritic/metacritic_crawler.py

# Send collected reviews to the running backend API
python crawling/send_to_api.py steam
python crawling/send_to_api.py metacritic
```

### Trigger AI Summarization

```bash
# POST to trigger async summarization for a game (game_id = integer)
curl -X POST http://localhost:8000/api/v1/games/{game_id}/summarize
# Force re-summarize ignoring cursor
curl -X POST "http://localhost:8000/api/v1/games/{game_id}/summarize?force=true"
```

## Architecture

### Data Flow

```
Crawlers → send_to_api.py → POST /api/v1/reviews/{platform}
    → backend stores ExternalReview rows in PostgreSQL

POST /api/v1/games/{id}/summarize
    → BackgroundTask: run_ai_pipeline_task()
    → Map stage: Ollama (gemma3:4b) chunks reviews → MapResult[]
    → Reduce stage: Groq API (llama-4-scout) → FinalSummary
    → Writes: game_review_summaries, playtime_analyses, critic_summaries
    → Invalidates Redis cache

GET /api/v1/games/{id}/summary → Redis cache → game_review_summaries
```

### Backend Structure (`backend/app/`)

- `main.py` — FastAPI app wiring; three router prefixes: `/api/v1/reviews`, `/api/v1/games` (summaries), `/api/v1/games` (analysis)
- `api/v1/reviews.py` — Ingestion endpoints for Steam and Metacritic review payloads
- `api/v1/summaries.py` — Game list, trigger summarization, fetch unified summary and regional perspectives
- `api/v1/analysis.py` — Playtime bucket analysis and critic summary endpoints
- `models/domain.py` — SQLAlchemy ORM models (single file for all tables)
- `services/ai_service.py` — Orchestrates the AI pipeline: incremental cursor logic, calling the pipeline, upserting results, reliability scoring
- `core/database.py` — Async SQLAlchemy engine and session
- `core/redis_client.py` — Redis cache helpers for summaries

### AI Pipeline (`ai-pipeline/ai_module/`)

- `map_reduce/pipeline.py` — `run_hybrid_summary_pipeline()`: entry point that coordinates map and reduce stages
- `map_reduce/map_local.py` — Map stage using Ollama (local LLM)
- `map_reduce/reduce_api.py` — Reduce stage using Groq API; produces `FinalSummary` Pydantic model
- `map_reduce/sampler.py` — Stratified review sampling; computes playtime buckets (p33/p66 percentiles)
- `map_reduce/chunker.py` — Chunks reviews by character count before map stage
- `map_reduce/rules.py` — Spam/quality filter rules applied before sampling
- `cache/redis_cache.py` — `RedisCache` used to cache map-stage chunk results
- `evaluation/` — Optional reliability scoring (Gemini, semantic similarity via sentence-transformers)

The backend mounts the AI pipeline at `/workspace/ai-pipeline` inside Docker (mapped from `./ai-pipeline`), and adds it to `sys.path` at startup.

### Database Schema

Managed via sequential SQL files in `database/`. The base schema is `01_schema.sql`; subsequent files are named `NN_migration_*.sql` and all are mounted as Docker init scripts (applied in filename order on first boot).

Key tables:
- `games` + `game_platform_map` — canonical game records with per-platform metadata (cover image, tags, scores stored as JSONB in `platform_meta_json`)
- `external_reviews` — all crawled reviews with `normalized_score_100` (0–100 scale) and `review_categories_json` (category + sentiment tags)
- `game_review_summaries` — AI-generated summaries; `is_current=TRUE` marks the active version; `summary_type='unified'` + `review_language IS NULL` for the main summary
- `playtime_analyses` — per-game early/mid/late bucket summaries
- `critic_summaries` — Metacritic critic-only summaries
- `game_summary_cursor` — tracks last-processed review ID per game for incremental pipeline runs
- `review_summary_jobs` — pipeline execution log with token counts and reliability metrics

### Frontend (`frontend/src/`)

Two-page SPA (React Router):
- `/` — `GameListPage.jsx`: game grid with cover images, tags, Metacritic rating (converted from 100→5 scale)
- `/games/:id` — `GameDetailPage.jsx`: unified summary, pros/cons/keywords, playtime bucket cards, critic summary section, representative review quotes

`VITE_API_BASE` env var controls the API host; empty string falls back to the Vite proxy (`/api → localhost:8000`). The frontend `.env` sets this for production nginx deployments.

## Key Design Decisions

- **Incremental summarization**: `game_summary_cursor` tracks the last processed `external_review.id`. The pipeline only processes reviews with `id > last_summarized_review_id`. `force=true` resets the cursor.
- **Hybrid LLM**: Ollama runs locally (map stage, cached in Redis), Groq API handles the expensive reduce stage. Map results are cached by chunk hash to avoid re-processing on re-runs.
- **Score normalization**: All review scores are normalized to 0–100 in `normalized_score_100`. Steam uses binary `is_recommended`; Metacritic critic scores are on 100-point scale; user scores are 10-point (×10).
- **Playtime buckets**: Computed dynamically at pipeline runtime using p33/p66 percentiles of the game's reviewer playtime distribution. Buckets with fewer than 30 reviews are stored as NULL.
