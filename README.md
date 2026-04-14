# Agentic Video Editor

A command-line AI video editor that turns raw footage and a creative brief into a polished ad. Point `ave` at a folder of clips, describe what you want, and an ensemble of AI agents handles the rest -- scene detection, shot selection, assembly, and quality review.

Built with Google Gemini for intelligence and FFmpeg for rendering.

> **Status:** The CLI is the primary, supported interface. A web UI (AVE Studio) is included but is a **work in progress** -- see [Web UI](#web-ui-work-in-progress) below.

## How It Works

```
Raw Footage + Creative Brief
        |
   [ Preprocess ]  -- scene detection, transcription, shot indexing
        |
   [ Director ]    -- AI agent picks shots, orders them, writes an EditPlan
        |
   [ Trim Refiner ] -- refines shot boundaries for tight cuts
        |
   [ Editor ]      -- renders the EditPlan to video via FFmpeg
        |
   [ Reviewer ]    -- scores the output (adherence, pacing, visual quality, watchability)
        |              if score < threshold, feeds back to Director and retries
        v
   Final Video + Review Scores
```

### Agents

| Agent | Role | Powered By |
|-------|------|------------|
| **Director** | Searches the footage index, selects shots, and produces an `EditPlan` with trimming, ordering, and text overlays | Google Gemini via ADK |
| **Trim Refiner** | Adjusts start/end trim points for tighter cuts | Gemini |
| **Editor** | Renders the EditPlan to an MP4 using FFmpeg/MoviePy | FFmpeg |
| **Reviewer** | Watches the rendered video and scores it on 5 dimensions (0-1 scale) | Gemini |

### Pipeline System

Pipelines are defined as YAML manifests in `pipelines/`. Each step names an agent and optionally a gate or retry condition:

```yaml
# pipelines/ugc-ad.yaml
steps:
  - agent: director
  - agent: trim_refiner
  - agent: editor
  - agent: reviewer
    retry_if:
      metric: overall
      threshold: 0.65           # retry if overall score < 0.65
      max_retries: 2            # up to 2 retries (3 total passes)
      feedback_target: director # send reviewer feedback back to director
```

Each retry iteration is saved as `{name}_v{N}.mp4` so you can compare versions.

### Style Templates

Style files in `styles/` give the Director structured guidance -- segment durations, pacing rules, text overlay placement, and music mood. The included `dtc-testimonial.yaml` defines a 30-second DTC ad structure (hook, problem, solution, social proof, CTA).

## Setup

### Prerequisites

- Python 3.11+
- FFmpeg installed and on PATH
- A [Google AI API key](https://aistudio.google.com/apikey) (for Gemini)

### 1. Clone and install

```bash
git clone https://github.com/poseljacob/agentic-video-editor.git
cd agentic-video-editor

python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
source .venv/bin/activate
```

### 2. Set up your API key

```bash
cp .env.example .env
# Edit .env and add your Google AI API key
```

### 3. Verify FFmpeg

```bash
ffmpeg -version
```

If not installed: `brew install ffmpeg` (macOS), `apt install ffmpeg` (Ubuntu), or [download](https://ffmpeg.org/download.html).

## Usage

The primary interface is the `ave` CLI. A single command takes you from raw footage to a finished cut.

### Basic run

```bash
ave edit \
  --footage-dir /path/to/your/footage \
  --brief '{"product": "My Product", "audience": "Women 25-45", "tone": "authentic", "duration_seconds": 30}' \
  --pipeline pipelines/ugc-ad.yaml \
  --style styles/dtc-testimonial.yaml
```

The brief can also be a path to a JSON file:

```bash
ave edit --footage-dir ./footage --brief brief.json
```

### What happens on a run

1. **Preprocess** -- scans your footage folder, detects scenes, transcribes speech, and writes a `footage_index.json`. Cached between runs.
2. **Director** -- searches the index and produces an `EditPlan` (ordered shots with trim points and text overlays).
3. **Trim Refiner** -- tightens each cut.
4. **Editor** -- renders the plan to MP4 via FFmpeg/MoviePy.
5. **Reviewer** -- scores the output across 5 dimensions; if below threshold, loops back to the Director with feedback.

Output lands in `output/` with versioned copies for each retry iteration.

### Creative Brief Schema

```json
{
  "product": "Product name",
  "audience": "Target demographic",
  "tone": "energetic, calm, professional, etc.",
  "duration_seconds": 30,
  "style_ref": "styles/dtc-testimonial.yaml"
}
```

### Review Scores

The Reviewer agent scores each output on five dimensions (0.0 - 1.0):

- **Adherence** -- does the edit follow the brief?
- **Pacing** -- are shot durations and energy levels well balanced?
- **Visual Quality** -- are cuts clean, transitions smooth?
- **Watchability** -- would a viewer watch to the end?
- **Overall** -- composite score

### Custom Pipelines

Create a YAML file in `pipelines/`:

```yaml
name: my-pipeline
steps:
  - agent: director
  - agent: editor
  - agent: reviewer
    retry_if:
      metric: overall
      threshold: 0.7
      max_retries: 3
```

Available agents: `director`, `trim_refiner`, `editor`, `reviewer`.

### Custom Styles

Create a YAML file in `styles/` with segment structure, text overlay rules, music mood, and pacing guidance. See `styles/dtc-testimonial.yaml` for the full format.

### Running Tests

```bash
pytest tests/
```

## Architecture

```
src/
  main.py        -- CLI entry point (`ave edit`)
  agents/        -- Director, Editor, Reviewer, TrimRefiner (Google ADK)
  models/        -- Pydantic schemas (CreativeBrief, Shot, EditPlan, ReviewScore)
  pipeline/      -- Preprocessing (scene detection + transcription) and pipeline runner
  tools/         -- Gemini tool functions (analyze footage, render, captions)
  web/           -- Optional FastAPI backend + Next.js frontend (work in progress)
```

## Web UI (Work In Progress)

An experimental web UI, **AVE Studio**, lives in `src/web/`. It wraps the same pipeline behind a FastAPI backend and a Next.js frontend styled as a traditional non-linear editor (project picker, source/program monitors, drag-and-drop timeline, media browser, inspector, review radar chart).

**This is pre-alpha and not the recommended way to use the project yet.** Expect rough edges, missing features, and breaking changes. The CLI is the supported path.

If you want to try it anyway:

```bash
# Prerequisites: Node.js 18+ and pnpm

# Install frontend deps
cd src/web/studio
pnpm install
cd ../../..

# Terminal 1: FastAPI backend
source .venv/bin/activate
uvicorn src.web.app:app --reload --port 8000

# Terminal 2: Next.js frontend
cd src/web/studio
pnpm dev --port 3000
```

Then open http://localhost:3000.

The backend exposes a REST API at `http://localhost:8000` (jobs, projects, footage, feedback, edit plan CRUD) plus a `/ws/jobs/{id}` WebSocket for real-time progress. See `src/web/routes/` for the full endpoint list.

## Project Structure

```
agentic-video-editor/
  .env.example          # API key template
  pyproject.toml        # Python project config
  pipelines/            # Pipeline YAML manifests
    ugc-ad.yaml
  styles/               # Style templates
    dtc-testimonial.yaml
  src/
    main.py             # CLI entry point
    agents/             # AI agents (Director, Editor, Reviewer, TrimRefiner)
    models/             # Pydantic data models
    pipeline/           # Preprocessing + pipeline runner
    tools/              # Gemini tool functions
    web/                # Experimental web UI (WIP)
      app.py            # FastAPI application
      jobs.py           # Background job registry
      routes/           # REST API endpoints
      studio/           # Next.js frontend
  tests/                # Test suite
```

## License

MIT
