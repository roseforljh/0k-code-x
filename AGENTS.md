# Repository Guidelines

## Project Structure & Module Organization
- `chatgpt_register.py`: core batch registration logic and token generation.
- `config.json`: runtime defaults (can be overridden by environment variables).
- `start_webui.py` and `start_webui.bat`: launch backend + frontend together.
- `webui/backend/`: FastAPI API service (`app.py`) and Python deps (`requirements.txt`).
- `webui/frontend/`: Vue 3 + Vite UI (`src/`, `package.json`).
- `codex_tokens/` and `output/`: generated runtime artifacts; do not commit secrets.
- `_tmp_*` directories are upstream/reference code snapshots; avoid changing them unless a task explicitly targets them.

## Build, Test, and Development Commands
- Install backend deps: `python -m pip install -r webui/backend/requirements.txt`
- Install core script dep: `python -m pip install curl_cffi`
- Run full local stack (recommended): `python start_webui.py`
- Windows shortcut: `start_webui.bat`
- Run backend only: `python -m uvicorn webui.backend.app:app --reload --host 127.0.0.1 --port 8000`
- Run frontend only: `npm --prefix webui/frontend run dev`
- Build frontend: `npm --prefix webui/frontend run build`
- Preview frontend build: `npm --prefix webui/frontend run preview`

## Coding Style & Naming Conventions
- Python: PEP 8, 4-space indentation, `snake_case` for functions/variables, `UPPER_SNAKE_CASE` for constants.
- Vue/JS: prefer composition API patterns already used in `App.vue`; keep variables/functions `camelCase`.
- Keep API routes under `/api/*` and use clear action names (example: `/api/tasks/{task_id}/stop`).
- Preserve existing file naming patterns (`*.py`, `App.vue`, `main.js`, `style.css`).

## Testing Guidelines
- There is no formal automated test suite in this snapshot.
- Before PRs, perform manual checks:
  - start stack with `python start_webui.py`
  - verify `GET /api/health` returns 200
  - verify task start/stop and account list flows in the UI
- If adding tests, place backend tests under `webui/backend/tests/` using `pytest` naming (`test_*.py`).

## Commit & Pull Request Guidelines
- Git history is not available in this exported workspace, so follow a consistent convention:
  - commit format: `type(scope): short summary` (example: `feat(webui): add token status refresh`)
  - keep commits focused and atomic
- PRs should include: purpose, key changes, manual verification steps, related issue (if any), and UI screenshots for frontend changes.

## Security & Configuration Tips
- Never commit real API keys, auth files, or generated token JSON.
- Use environment variables for sensitive values (`CFEMAIL_PASSWORD`, `UPLOAD_API_TOKEN`, etc.).
- Treat `output/` and `codex_tokens/` as local runtime data.
