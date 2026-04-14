# Agentic Video Editor

AI-powered video editor that turns raw footage and a creative brief into a polished ad. Point it at a folder of clips, describe what you want, and an ensemble of AI agents handles the rest -- scene detection, shot selection, assembly, and quality review.

Built with Google Gemini for intelligence and FFmpeg for rendering.

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
    gate: human_approval        # pause for human review (web UI only)
  - agent: trim_refiner
  - agent: editor
  - agent: reviewer
    retry_if:
      metric: overall
      threshold: 0.65           # retry if overall score < 0.65
      max_retries: 2            # up to 2 retries (3 total passes)
      feedback_target: director # send reviewer feedback back to director
```

### Style Templates

Style files in `styles/` give the Director structured guidance -- segment durations, pacing rules, text overlay placement, and music mood. The included `dtc-testimonial.yaml` defines a 30-second DTC ad structure (hook, problem, solution, social proof, CTA).

## Architecture

```
src/
  agents/        -- Director, Editor, Reviewer, TrimRefiner (Google ADK)
  models/        -- Pydantic schemas (CreativeBrief, Shot, EditPlan, ReviewScore)
  pipeline/      -- Preprocessing (scene detection + transcription) and pipeline runner
  tools/         -- Gemini tool functions (analyze footage, render, captions)
  web/
    app.py       -- FastAPI backend (REST API + WebSocket)
    routes/      -- API endpoints (jobs, projects, footage, feedback, etc.)
    jobs.py      -- Background job registry
    studio/      -- Next.js frontend (NLE-style editor UI)
```

### Web UI (AVE Studio)

The frontend is a Next.js app styled as a traditional non-linear editor:

- **Project Picker** -- create projects by pointing at any folder of video files
- **Source Monitor** -- preview individual clips from the media browser
- **Program Monitor** -- watch the final rendered output
- **Timeline** -- drag-to-reorder clips, view roll types, durations
- **Media Browser** -- searchable shot catalog with roll-type filters
- **Inspector** -- trim controls, text overlays, transitions, review radar chart
- **Console** -- real-time pipeline progress via WebSocket
- **Chat Panel** -- send feedback to trigger revision iterations

**Tech stack:** Next.js 16, React 19, TypeScript, Tailwind CSS 4, Zustand, dnd-kit, Recharts, Lucide icons.

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+ and pnpm
- FFmpeg installed and on PATH
- A [Google AI API key](https://aistudio.google.com/apikey) (for Gemini)

### 1. Clone and install Python dependencies

```bash
git clone https://github.com/yourusername/agentic-video-editor.git
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

### 3. Install the frontend

```bash
cd src/web/studio
pnpm install
cd ../../..
```

### 4. Verify FFmpeg

```bash
ffmpeg -version
```

If not installed: `brew install ffmpeg` (macOS), `apt install ffmpeg` (Ubuntu), or [download](https://ffmpeg.org/download.html).

## Usage

### CLI

```bash
# Preprocess footage (scene detection + transcription)
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

### Web UI

Start both the backend and frontend:

```bash
# Terminal 1: FastAPI backend
source .venv/bin/activate
uvicorn src.web.app:app --reload --port 8000

# Terminal 2: Next.js frontend
cd src/web/studio
pnpm dev --port 3000
```

Open http://localhost:3000, create a project by browsing to a footage folder, and run a pipeline.

### API

The FastAPI backend exposes a full REST API at `http://localhost:8000`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/projects` | GET/POST | List or create projects |
| `/api/projects/{id}` | GET/DELETE | Get or delete a project |
| `/api/browse?path=` | GET | Browse filesystem directories |
| `/api/jobs` | GET/POST | List jobs or start a pipeline run |
| `/api/jobs/{id}` | GET | Get job status and result |
| `/api/jobs/{id}/edit-plan` | GET/PUT | Read or update the edit plan |
| `/api/jobs/{id}/feedback` | POST | Submit feedback for revision |
| `/api/jobs/{id}/re-render` | POST | Re-render with modified edit plan |
| `/api/jobs/{id}/review-only` | POST | Run reviewer on existing output |
| `/api/footage/catalog` | GET | List all indexed shots |
| `/api/footage/search` | GET | Semantic search across footage |
| `/ws/jobs/{id}` | WS | Real-time pipeline progress stream |

### Running Tests

```bash
pytest tests/
```

## Configuration

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
    web/
      app.py            # FastAPI application
      jobs.py           # Background job registry
      routes/           # REST API endpoints
      studio/           # Next.js frontend
        src/
          app/          # Pages (project picker, editor)
          components/   # UI components
          stores/       # Zustand state management
          hooks/        # WebSocket streaming
          lib/          # API client, utilities
          types/        # TypeScript schemas
  tests/                # Test suite
```

## License

MIT
