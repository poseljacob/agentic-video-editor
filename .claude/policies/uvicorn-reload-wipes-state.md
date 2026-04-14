---
id: uvicorn-reload-wipes-state
title: uvicorn --reload wipes in-memory registries mid-run
scope: repo
trigger: editing backend source while a pipeline is running
enforcement: soft
---

## Rule

Do NOT edit files under `src/` (especially `src/web/`, `src/pipeline/`, `src/agents/`) while a pipeline job is running against `uvicorn --reload`. The file-watcher restarts the process, which wipes `JobRegistry`, `ProjectStore`, and any other in-memory state. You will lose all in-flight jobs and have to re-create projects from scratch.

If you need to modify backend code mid-session:
1. Let the current job finish (or cancel it)
2. Make the edit
3. Recreate the project and restart the pipeline

For tests that cross the reload boundary, run `uvicorn` without `--reload` and restart manually.

## Rationale

`JobRegistry` and `ProjectStore` are populated at FastAPI `lifespan` startup and held only in `app.state`. `--reload` triggers a full process restart on any `.py` change under the watched dirs, dropping all state. This bit the developer during open-source-prep: a runner.py edit mid-pipeline killed the active Demo Project and its in-flight reviewer retry loop, forcing a full re-run.
