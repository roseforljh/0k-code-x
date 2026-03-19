import base64
import binascii
import json
import os
import sys
import time
import uuid
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, CancelledError, as_completed
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import chatgpt_register as core


class StartTaskRequest(BaseModel):
    total_accounts: int = Field(default=3, ge=1, le=1000)
    max_workers: int = Field(default=3, ge=1, le=100)
    proxy: Optional[str] = None
    output_file: Optional[str] = "registered_accounts.txt"


class StopTaskResponse(BaseModel):
    task_id: str
    status: str


class AccountUpsertRequest(BaseModel):
    email: str = Field(min_length=1)
    account_password: str = Field(min_length=1)
    email_password: Optional[str] = ""
    oauth: Optional[str] = ""


class AccountBatchDeleteRequest(BaseModel):
    emails: List[str] = Field(default_factory=list)


class ExportAccountsRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100000)


class DetectSettingsRequest(BaseModel):
    detect_base_url: Optional[str] = ""
    detect_api_key: Optional[str] = ""


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class CodexProxyCheckRequest(BaseModel):
    api_base_url: Optional[str] = ""
    api_key: Optional[str] = ""


class PushCodexTokenSingleRequest(BaseModel):
    api_base_url: Optional[str] = ""
    api_key: Optional[str] = ""
    filename: str = Field(min_length=1)
    delete_local_after_upload: bool = True


@dataclass
class AutoMaintainState:
    enabled: bool = False
    running: bool = False
    interval_seconds: int = 1800
    target_count: int = 100
    max_workers: int = 3
    remote_valid_count: int = 0
    last_started_at: Optional[float] = None
    last_finished_at: Optional[float] = None
    last_error: str = ""
    logs: List[str] = field(default_factory=list)


@dataclass
class TaskState:
    task_id: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    total_accounts: int = 0
    max_workers: int = 0
    proxy: Optional[str] = None
    output_file: str = "registered_accounts.txt"
    started_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    completed_count: int = 0
    logs: List[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    log_queue: Queue = field(default_factory=Queue)
    active_workers: Dict[int, object] = field(default_factory=dict)


app = FastAPI(title="ChatGPT Register WebUI API")

AUTH_COOKIE_NAME = "okx_session"

def _get_panel_auth_settings() -> tuple[str, str]:
    username = (os.environ.get("PANEL_LOGIN_USERNAME") or "admin").strip()
    password = (os.environ.get("PANEL_LOGIN_PASSWORD") or "CHANGE_ME").strip()
    return username, password

def _is_authenticated(request: Request) -> bool:
    cookie = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    user, pwd = _get_panel_auth_settings()
    expected = f"{user}:{pwd}"
    return cookie == expected

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def panel_auth_middleware(request: Request, call_next):
    path = request.url.path or "/"
    if path.startswith("/assets/") or path in {"/favicon.ico", "/api/health", "/api/auth/login", "/api/auth/session", "/api/auth/logout"}:
        return await call_next(request)
    if path.startswith("/api/") and not _is_authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
def startup_auto_maintain():
    t = threading.Thread(target=_auto_maintain_loop, daemon=True)
    t.start()

_task_lock = threading.Lock()
_tasks: Dict[str, TaskState] = {}
_account_file_lock = threading.Lock()
_strict_status_cache_lock = threading.Lock()
_strict_status_cache: Dict[str, dict] = {}
_token_index_cache_lock = threading.Lock()
_token_index_cache: Dict[str, Any] = {"built_at": 0.0, "index": {}}
_detect_settings_lock = threading.Lock()
_auto_maintain_lock = threading.Lock()
_auto_maintain_state = AutoMaintainState()


def _read_required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _normalize_base_url(v: Optional[str]) -> str:
    s = (v or "").strip()
    return s[:-1] if s.endswith("/") else s


def _load_detect_settings_from_env() -> dict:
    return {
        "detect_base_url": _normalize_base_url(os.environ.get("REMOTE_API_BASE_URL")),
        "detect_api_key": (os.environ.get("REMOTE_API_KEY") or "").strip(),
    }


_detect_settings = _load_detect_settings_from_env()


def _join_base_url(base: str, path: str) -> str:
    b = _normalize_base_url(base)
    p = "/" + str(path or "").lstrip("/")
    return f"{b}{p}" if b else p


def _management_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "chatgpt-register-webui/token-pusher",
    }


def _upload_codex_token_file_to_cliproxy(base_url: str, api_key: str, filename: str, content: bytes):
    target_url = _join_base_url(base_url, "/v0/management/auth-files")
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    crlf = b"\r\n"
    body = b"".join([
        f"--{boundary}".encode("utf-8"), crlf,
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode("utf-8"), crlf,
        b"Content-Type: application/json", crlf, crlf,
        content, crlf,
        f"--{boundary}--".encode("utf-8"), crlf,
    ])

    headers = _management_headers(api_key)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(url=target_url, method="POST", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        code = getattr(resp, "status", 200)
        raw = resp.read() or b""
        text = raw.decode("utf-8", errors="ignore")
        return code, text


def _get_remote_auth_files(base_url: str, api_key: str):
    target_url = _join_base_url(base_url, "/v0/management/auth-files")
    req = urllib.request.Request(url=target_url, method="GET", headers=_management_headers(api_key))
    with urllib.request.urlopen(req, timeout=12) as resp:
        code = getattr(resp, "status", 200)
        raw = resp.read() or b""
        text = raw.decode("utf-8", errors="ignore")
        if not (200 <= int(code) < 300):
            return []
        try:
            body = json.loads(text) if text else {}
        except Exception:
            return []

        if isinstance(body, list):
            return body

        if isinstance(body, dict):
            files = body.get("files")
            if isinstance(files, list):
                return files
            data = body.get("data")
            if isinstance(data, dict) and isinstance(data.get("files"), list):
                return data.get("files")
            if isinstance(data, list):
                return data

        return []


def _list_local_codex_token_files() -> List[dict]:
    token_dir = core._TOKEN_DIR
    if not os.path.isdir(token_dir):
        return []
    rows: List[dict] = []
    for name in os.listdir(token_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(token_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if str(payload.get("type") or "").strip().lower() != "codex":
                continue
            rows.append({
                "name": name,
                "path": path,
                "mtime": os.path.getmtime(path),
                "size": os.path.getsize(path),
                "email": str(payload.get("email") or "").strip(),
            })
        except Exception:
            continue
    rows.sort(key=lambda x: float(x.get("mtime") or 0), reverse=True)
    return rows


def _get_local_codex_token_file_by_name(filename: str):
    safe = os.path.basename(filename or "")
    if not safe or safe != filename:
        return None
    for row in _list_local_codex_token_files():
        if row.get("name") == safe:
            return row
    return None


def _get_detect_settings() -> dict:
    with _detect_settings_lock:
        return {
            "detect_base_url": _detect_settings.get("detect_base_url", ""),
            "detect_api_key": _detect_settings.get("detect_api_key", ""),
        }


def _update_detect_settings(base_url: Optional[str], api_key: Optional[str]) -> dict:
    raise HTTPException(status_code=403, detail="detect settings are managed by environment variables")


def _read_env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return max(minimum, value)


def _read_env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _get_cliproxy_management_settings(override_base_url: Optional[str] = None, override_api_key: Optional[str] = None) -> tuple[str, str]:
    env_base = _normalize_base_url(os.environ.get("CLIPROXY_API_BASE_URL") or "")
    env_key = (os.environ.get("CLIPROXY_API_KEY") or "").strip()
    base_url = _normalize_base_url((override_base_url or "").strip()) or env_base
    api_key = (override_api_key or "").strip() or env_key
    return base_url, api_key


def _load_auto_maintain_settings() -> dict:
    return {
        "enabled": _read_env_bool("AUTO_MAINTAIN_ENABLED", True),
        "interval_seconds": _read_env_int("AUTO_MAINTAIN_INTERVAL_SECONDS", 1800),
        "target_count": _read_env_int("AUTO_MAINTAIN_TARGET_COUNT", 100),
        "max_workers": _read_env_int("AUTO_MAINTAIN_MAX_WORKERS", 3),
        "api_base_url": _normalize_base_url(os.environ.get("CLIPROXY_API_BASE_URL") or ""),
        "api_key": (os.environ.get("CLIPROXY_API_KEY") or "").strip(),
    }


def _auto_log(message: str):
    with _auto_maintain_lock:
        _auto_maintain_state.logs.append(message)
        if len(_auto_maintain_state.logs) > 500:
            _auto_maintain_state.logs = _auto_maintain_state.logs[-500:]


def _delete_local_account_and_tokens_by_email(email: str):
    key = _normalize_email(email)
    if not key:
        return
    with _account_file_lock:
        rows = _read_accounts_raw()
        new_rows = [r for r in rows if _normalize_email(r.get("email", "")) != key]
        if len(new_rows) != len(rows):
            _write_accounts_raw(new_rows)
        _delete_tokens_for_email(key)


def _delete_local_account_and_tokens_by_filename(filename: str):
    safe_name = os.path.basename(filename or "")
    if not safe_name:
        return
    row = _get_local_codex_token_file_by_name(safe_name)
    if row and row.get("email"):
        _delete_local_account_and_tokens_by_email(str(row.get("email") or ""))
        return
    if safe_name.endswith(".json"):
        stem = safe_name[:-5]
        email = stem.rsplit("-", 1)[0].strip() if "-" in stem else ""
        if email:
            _delete_local_account_and_tokens_by_email(email)


def _push_local_codex_file_to_remote(base_url: str, api_key: str, filename: str) -> bool:
    row = _get_local_codex_token_file_by_name(filename)
    if not row:
        return False
    with open(str(row.get("path")), "rb") as f:
        content = f.read()
    code, _text = _upload_codex_token_file_to_cliproxy(base_url, api_key, filename, content)
    return 200 <= int(code) < 300


def _list_remote_codex_names(base_url: str, api_key: str) -> List[str]:
    remote_files = _get_remote_auth_files(base_url, api_key)
    names: List[str] = []
    for item in remote_files:
        if not isinstance(item, dict):
            continue
        provider_type = str(item.get("type") or item.get("provider") or "").strip().lower()
        if provider_type != "codex":
            continue
        name = str(item.get("name") or item.get("file_name") or item.get("filename") or "").strip()
        if name:
            names.append(name)
    return names


def _collect_remote_codex_status(base_url: str, api_key: str) -> List[Dict[str, Any]]:
    remote_files = _get_remote_auth_files(base_url, api_key)
    candidates: List[Dict[str, Any]] = []
    for item in remote_files:
        if not isinstance(item, dict):
            continue
        provider_type = str(item.get("type") or item.get("provider") or "").strip().lower()
        if provider_type != "codex":
            continue
        name = str(item.get("name") or item.get("file_name") or item.get("filename") or "").strip()
        if name:
            candidates.append({"name": name, "item": item})

    if not candidates:
        return []

    worker_count = min(max(1, len(candidates)), 12)
    ordered: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_check_one_remote_codex_file, base_url, api_key, c["name"], c["item"]): idx
            for idx, c in enumerate(candidates)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                ordered[idx] = future.result()
            except Exception as ex:
                ordered[idx] = {
                    "name": candidates[idx]["name"],
                    "is_invalid": True,
                    "status_code": None,
                    "message": f"check failed: {ex}",
                }
    return [ordered[i] for i in range(len(candidates)) if i in ordered]


def _delete_remote_codex_files(base_url: str, api_key: str, filenames: List[str]) -> List[str]:
    import urllib.parse

    deleted: List[str] = []
    for name in filenames:
        target_url = _join_base_url(base_url, f"/v0/management/auth-files?name={urllib.parse.quote(name)}")
        del_req = urllib.request.Request(url=target_url, method="DELETE", headers=_management_headers(api_key))
        with urllib.request.urlopen(del_req, timeout=10) as resp:
            if 200 <= getattr(resp, "status", 200) < 300:
                deleted.append(name)
    return deleted


def _collect_local_account_statuses(strict: bool = True) -> List[Dict[str, Any]]:
    rows = _read_accounts_raw()
    if not rows:
        return []
    token_index = _build_token_index(force=True)
    results: List[Dict[str, Any]] = []
    worker_count = min(max(1, len(rows)), 16)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(_get_token_status_for_email, str(r.get("email") or ""), strict, token_index): r for r in rows}
        for future in as_completed(future_map):
            row = future_map[future]
            email = str(row.get("email") or "")
            try:
                status = future.result()
            except Exception as ex:
                status = {"status": "unknown", "message": f"local strict check failed: {ex}", "error_code": "42302"}
            results.append({"email": email, "token_status": status})
    return results


def _cleanup_invalid_local_accounts(strict: bool = True) -> Dict[str, Any]:
    statuses = _collect_local_account_statuses(strict=strict)
    removed = []
    for item in statuses:
        email = str(item.get("email") or "")
        status = str(((item.get("token_status") or {}).get("status") or "unknown")).lower()
        if status in {"expired", "invalid", "missing"}:
            _delete_local_account_and_tokens_by_email(email)
            removed.append({"email": email, "status": status})
    return {"checked": len(statuses), "removed": removed, "remaining": max(0, len(statuses) - len(removed))}


def _perform_auto_maintain_once():
    settings = _load_auto_maintain_settings()
    base_url = settings.get("api_base_url") or ""
    api_key = settings.get("api_key") or ""
    if not base_url or not api_key:
        raise RuntimeError("missing CLIPROXY_API_BASE_URL or CLIPROXY_API_KEY")

    _auto_log("[auto] checking local accounts")
    local_cleanup = _cleanup_invalid_local_accounts(strict=True)
    if local_cleanup.get("removed"):
        _auto_log(f"[auto] removed invalid local accounts: {len(local_cleanup['removed'])}")
    else:
        _auto_log(f"[auto] local accounts checked: {local_cleanup.get('checked', 0)}, removed: 0")

    _auto_log("[auto] checking remote codex status")
    results = _collect_remote_codex_status(base_url, api_key)
    invalid_names = [x.get("name") for x in results if int(x.get("status_code") or 0) != 200 and x.get("name")]

    if invalid_names:
        _auto_log(f"[auto] deleting invalid remote codex files: {len(invalid_names)}")
        deleted = _delete_remote_codex_files(base_url, api_key, invalid_names)
        for name in deleted:
            _delete_local_account_and_tokens_by_filename(name)
        _auto_log(f"[auto] deleted invalid remote codex files: {len(deleted)}")

    status_rows = _collect_remote_codex_status(base_url, api_key)
    remote_valid_count = sum(1 for x in status_rows if int(x.get("status_code") or 0) == 200 and not x.get("is_invalid"))

    while remote_valid_count < int(settings["target_count"]):
        missing = int(settings["target_count"]) - remote_valid_count
        batch_size = min(int(settings["max_workers"]), missing)
        _auto_log(f"[auto] remote valid count={remote_valid_count}, registering batch={batch_size}")

        output_file = _resolve_output_file("registered_accounts.txt")
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        created_files_before = {x.get("name") for x in _list_local_codex_token_files() if x.get("name")}
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = [executor.submit(core._register_one, idx + 1, batch_size, None, output_file) for idx in range(batch_size)]
            for future in as_completed(futures):
                ok, _email, err = future.result()
                if not ok:
                    _auto_log(f"[auto] register failed: {err}")

        local_cleanup = _cleanup_invalid_local_accounts(strict=True)
        if local_cleanup.get("removed"):
            _auto_log(f"[auto] post-register removed invalid local accounts: {len(local_cleanup['removed'])}")

        created_files_after = {x.get("name") for x in _list_local_codex_token_files() if x.get("name")}
        new_files = [name for name in created_files_after if name not in created_files_before]
        for name in new_files:
            try:
                if _push_local_codex_file_to_remote(base_url, api_key, name):
                    _auto_log(f"[auto] pushed token: {name}")
            except Exception as ex:
                _auto_log(f"[auto] push failed for {name}: {ex}")

        status_rows = _collect_remote_codex_status(base_url, api_key)
        remote_valid_count = sum(1 for x in status_rows if int(x.get("status_code") or 0) == 200 and not x.get("is_invalid"))

    with _auto_maintain_lock:
        _auto_maintain_state.remote_valid_count = remote_valid_count


def _auto_maintain_loop():
    while True:
        settings = _load_auto_maintain_settings()
        with _auto_maintain_lock:
            _auto_maintain_state.enabled = bool(settings["enabled"])
            _auto_maintain_state.interval_seconds = int(settings["interval_seconds"])
            _auto_maintain_state.target_count = int(settings["target_count"])
            _auto_maintain_state.max_workers = int(settings["max_workers"])

        if settings["enabled"]:
            with _auto_maintain_lock:
                _auto_maintain_state.running = True
                _auto_maintain_state.last_started_at = time.time()
                _auto_maintain_state.last_error = ""
            try:
                _perform_auto_maintain_once()
            except Exception as ex:
                with _auto_maintain_lock:
                    _auto_maintain_state.last_error = str(ex)
                _auto_log(f"[auto] failed: {ex}")
            finally:
                with _auto_maintain_lock:
                    _auto_maintain_state.running = False
                    _auto_maintain_state.last_finished_at = time.time()

        time.sleep(max(5, int(settings["interval_seconds"])))


def _push_log(task: TaskState, line: str):
    if line is None:
        return
    line = line.rstrip("\r")
    if not line:
        return
    with _task_lock:
        task.logs.append(line)
        if len(task.logs) > 2000:
            task.logs = task.logs[-2000:]
    task.log_queue.put(line)


def _register_active_worker(task: TaskState, idx: int, reg):
    with _task_lock:
        task.active_workers[idx] = reg


def _unregister_active_worker(task: TaskState, idx: int):
    with _task_lock:
        task.active_workers.pop(idx, None)


def _force_abort_active_workers(task: TaskState):
    with _task_lock:
        workers = list(task.active_workers.items())
    for idx, reg in workers:
        try:
            if hasattr(reg, "session") and reg.session:
                reg.session.close()
            _push_log(task, f"[TASK] account={idx} worker_abort_signal")
        except Exception as ex:
            _push_log(task, f"[TASK] account={idx} worker_abort_error: {ex}")


def _force_abort_active_workers_async(task: TaskState):
    threading.Thread(target=_force_abort_active_workers, args=(task,), daemon=True).start()


def _resolve_output_file(output_file: Optional[str]) -> str:
    target = output_file or "registered_accounts.txt"
    if os.path.isabs(target):
        return target
    return os.path.join(core._OUTPUT_DIR, target)


def _accounts_file_path() -> str:
    return _resolve_output_file("registered_accounts.txt")


def _parse_account_line(line: str):
    raw = line.strip()
    if not raw:
        return None
    parts = raw.split("----")
    if len(parts) < 2:
        return None
    email = parts[0].strip()
    account_password = parts[1].strip()
    email_password = parts[2].strip() if len(parts) > 2 else ""
    oauth = ""
    if len(parts) > 3 and "=" in parts[3]:
        oauth = parts[3].split("=", 1)[1].strip()
    return {
        "email": email,
        "account_password": account_password,
        "email_password": email_password,
        "oauth": oauth,
    }


def _read_accounts_raw():
    rows = []
    reg_path = _accounts_file_path()
    if not os.path.exists(reg_path):
        return rows
    with open(reg_path, "r", encoding="utf-8") as f:
        for line in f:
            row = _parse_account_line(line)
            if row:
                rows.append(row)
    return rows


def _read_accounts():
    rows = _read_accounts_raw()
    data = []
    for idx, row in enumerate(rows, start=1):
        item = dict(row)
        item["index"] = idx
        data.append(item)
    data.reverse()
    return data


def _line_for_account(row: dict) -> str:
    oauth = row.get("oauth", "")
    oauth_part = f"oauth={oauth}" if oauth else "oauth="
    return f"{row.get('email', '').strip()}----{row.get('account_password', '').strip()}----{row.get('email_password', '').strip()}----{oauth_part}"


def _write_accounts_raw(rows: List[dict]):
    os.makedirs(core._OUTPUT_DIR, exist_ok=True)
    reg_path = _accounts_file_path()
    with open(reg_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(_line_for_account(row) + "\n")


def _normalize_email(v: str) -> str:
    return (v or "").strip().lower()


def _build_token_index(force: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    now = time.time()
    with _token_index_cache_lock:
        cached_index = _token_index_cache.get("index")
        built_at = float(_token_index_cache.get("built_at") or 0.0)
        if (not force) and isinstance(cached_index, dict) and (now - built_at) <= 5.0:
            return cached_index

    token_dir = core._TOKEN_DIR
    if not os.path.isdir(token_dir):
        with _token_index_cache_lock:
            _token_index_cache["index"] = {}
            _token_index_cache["built_at"] = now
        return {}

    index: Dict[str, List[Dict[str, Any]]] = {}
    for name in os.listdir(token_dir):
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        if "-" not in stem:
            continue
        email = stem.rsplit("-", 1)[0].strip()
        if not email:
            continue
        path = os.path.join(token_dir, name)
        try:
            row = {
                "name": name,
                "size": os.path.getsize(path),
                "mtime": os.path.getmtime(path),
            }
        except OSError:
            continue
        index.setdefault(email, []).append(row)

    for email in index:
        index[email].sort(key=lambda x: x["mtime"], reverse=True)

    with _token_index_cache_lock:
        _token_index_cache["index"] = index
        _token_index_cache["built_at"] = now
    return index


def _token_files_for_email(email: str, token_index: Optional[Dict[str, List[Dict[str, Any]]]] = None, status_cache: Optional[Dict[str, dict]] = None):
    token_dir = core._TOKEN_DIR
    if not os.path.isdir(token_dir):
        return []

    rows = []
    source_files = token_index.get(email, []) if isinstance(token_index, dict) else None
    if source_files is None:
        prefix = f"{email}-"
        source_files = []
        for name in os.listdir(token_dir):
            if not name.endswith(".json"):
                continue
            if not name.startswith(prefix):
                continue
            path = os.path.join(token_dir, name)
            try:
                source_files.append({
                    "name": name,
                    "size": os.path.getsize(path),
                    "mtime": os.path.getmtime(path),
                })
            except OSError:
                continue
        source_files.sort(key=lambda x: x["mtime"], reverse=True)

    for meta in source_files:
        name = meta.get("name")
        if not name:
            continue
        file_status = status_cache.get(name) if isinstance(status_cache, dict) else None
        if not isinstance(file_status, dict):
            file_status = _get_token_status_for_file(name)
            if isinstance(status_cache, dict):
                status_cache[name] = file_status
        rows.append({
            "name": name,
            "size": meta.get("size", 0),
            "mtime": meta.get("mtime", 0),
            "status": file_status.get("status"),
            "message": file_status.get("message"),
            "error_code": file_status.get("error_code"),
            "expired_at": file_status.get("expired_at"),
        })

    return rows


def _read_token_json(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="invalid token filename")
    path = os.path.join(core._TOKEN_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="token file not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_iso_datetime(value: str):
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _decode_jwt_payload(token: str):
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment.encode("utf-8"))
        return json.loads(decoded.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _strict_verify_access_token(access_token: str, timeout: float = 2.2):
    if not isinstance(access_token, str) or not access_token.strip():
        return {"ok": False, "message": "missing access_token", "http_status": None, "error_code": "42210"}

    settings = _get_detect_settings()
    base = settings.get("detect_base_url") or ""
    key = settings.get("detect_api_key") or ""

    if base:
        base_norm = _normalize_base_url(base)
        model_urls = [
            _join_base_url(base_norm, "/v1/models"),
            _join_base_url(base_norm, "/models"),
            _join_base_url(base_norm, "/api/v1/models"),
            _join_base_url(base_norm, "/openai/v1/models"),
        ]
    else:
        model_urls = ["https://api.openai.com/v1/models"]

    model_urls = list(dict.fromkeys(model_urls))

    headers = {
        "Authorization": f"Bearer {access_token.strip()}",
        "User-Agent": "chatgpt-register-webui/strict-check",
    }
    if key:
        headers["X-API-Key"] = key

    last_result = None
    for target_url in model_urls:
        req = urllib.request.Request(url=target_url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", 200)
                if 200 <= code < 300:
                    return {"ok": True, "message": "strict check passed", "http_status": code, "error_code": "20000"}
                last_result = {"ok": False, "message": f"upstream status {code}", "http_status": code, "error_code": "42299"}
        except urllib.error.HTTPError as e:
            code = getattr(e, "code", None)
            if code == 404 and len(model_urls) > 1 and target_url != model_urls[-1]:
                last_result = {"ok": False, "message": "resource not found", "http_status": code, "error_code": "42204"}
                continue
            if code == 401:
                return {"ok": False, "message": "unauthorized token", "http_status": code, "error_code": "42201"}
            if code in (402, 403):
                return {"ok": False, "message": "permission/subscription denied", "http_status": code, "error_code": "42202"}
            if code == 429:
                return {"ok": False, "message": "rate or quota limited", "http_status": code, "error_code": "42203"}
            if code == 404:
                return {"ok": False, "message": "resource not found", "http_status": code, "error_code": "42204"}
            if code and 500 <= code < 600:
                return {"ok": False, "message": "upstream server error", "http_status": code, "error_code": "42301"}
            return {"ok": False, "message": f"request failed ({code})", "http_status": code, "error_code": "42299"}
        except Exception as e:
            return {"ok": False, "message": f"network error: {e}", "http_status": None, "error_code": "42302"}

    return last_result or {"ok": False, "message": "request failed", "http_status": None, "error_code": "42299"}
def _parse_id_token_payload(value: Any):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return _decode_jwt_payload(s)


def _extract_remote_token_fields(payload: Any, max_depth: int = 4) -> Dict[str, str]:
    """Best-effort extraction for token fields from heterogeneous remote auth-file payloads."""
    if max_depth < 0:
        return {}

    result: Dict[str, str] = {}

    def _pick(d: Dict[str, Any], key: str, *aliases: str):
        for k in (key, *aliases):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                result[key] = v.strip()
                return True
        return False

    if isinstance(payload, dict):
        _pick(payload, "access_token", "accessToken", "token")
        _pick(payload, "id_token", "idToken")
        _pick(payload, "refresh_token", "refreshToken")
        _pick(payload, "account_id", "accountId", "chatgpt_account_id", "chatgptAccountId")

        # Try common wrapper keys used by management APIs.
        nested_keys = ["data", "content", "payload", "file", "auth", "token_data", "tokenData", "body"]
        for key in nested_keys:
            if "access_token" in result:
                break
            if key not in payload:
                continue
            sub = payload.get(key)
            # Some APIs return JSON string in "content/body".
            if isinstance(sub, str):
                s = sub.strip()
                if s.startswith("{") and s.endswith("}"):
                    try:
                        sub = json.loads(s)
                    except Exception:
                        sub = None
            sub_fields = _extract_remote_token_fields(sub, max_depth=max_depth - 1)
            for fk, fv in sub_fields.items():
                if fk not in result and isinstance(fv, str) and fv.strip():
                    result[fk] = fv.strip()
    elif isinstance(payload, str):
        s = payload.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                return _extract_remote_token_fields(json.loads(s), max_depth=max_depth - 1)
            except Exception:
                return {}

    return result


def _resolve_chatgpt_account_id(token_data: Dict[str, Any]) -> Optional[str]:
    payload = _parse_id_token_payload(token_data.get("id_token"))
    if not isinstance(payload, dict):
        return None

    value = payload.get("chatgpt_account_id") or payload.get("chatgptAccountId")
    if isinstance(value, str) and value.strip():
        return value.strip()

    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        nested = auth.get("chatgpt_account_id") or auth.get("chatgptAccountId")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _pick_week_window(rate_limit: Optional[Dict[str, Any]]):
    if not isinstance(rate_limit, dict):
        return None
    primary = rate_limit.get("primary_window") or rate_limit.get("primaryWindow")
    secondary = rate_limit.get("secondary_window") or rate_limit.get("secondaryWindow")

    def _window_seconds(win):
        if not isinstance(win, dict):
            return None
        return _to_float(win.get("limit_window_seconds") or win.get("limitWindowSeconds"))

    for win in (primary, secondary):
        if _window_seconds(win) == 604800:
            return win

    if isinstance(secondary, dict):
        return secondary
    if isinstance(primary, dict):
        return primary
    return None


def _build_quota_card(rate_limit: Optional[Dict[str, Any]], label: str, missing_code: str, http_status=None):
    window = _pick_week_window(rate_limit)
    if not isinstance(window, dict):
        return {
            "ok": False,
            "remaining_percent": None,
            "used_percent": None,
            "reset_after_seconds": None,
            "reset_at": None,
            "error_code": missing_code,
            "http_status": http_status,
            "message": f"{label}鏁版嵁缂哄け",
        }

    remaining = _to_float(window.get("remaining_percent") or window.get("remainingPercent"))
    used = _to_float(window.get("used_percent") or window.get("usedPercent"))
    limit_reached = bool((rate_limit or {}).get("limit_reached") or (rate_limit or {}).get("limitReached"))
    allowed = (rate_limit or {}).get("allowed")

    if remaining is not None:
        remaining = max(0.0, min(100.0, remaining))
        used = round(max(0.0, 100.0 - remaining), 2)
    else:
        if used is not None:
            remaining = max(0.0, min(100.0, used))
            used = round(max(0.0, 100.0 - remaining), 2)
        elif limit_reached or allowed is False:
            remaining = 0.0
            used = 100.0
        else:
            remaining = None

    reset_after = _to_float(window.get("reset_after_seconds") or window.get("resetAfterSeconds"))
    reset_at = _to_float(window.get("reset_at") or window.get("resetAt"))

    return {
        "ok": True,
        "remaining_percent": remaining,
        "used_percent": used,
        "reset_after_seconds": int(reset_after) if reset_after is not None else None,
        "reset_at": int(reset_at) if reset_at is not None else None,
        "error_code": "20000",
        "http_status": http_status,
        "message": f"{label}姝ｅ父",
    }


def _strict_fetch_codex_usage(access_token: str, account_id: str, timeout: float = 2.2):
    if not isinstance(access_token, str) or not access_token.strip():
        return {"ok": False, "message": "missing access_token", "http_status": None, "error_code": "42210"}
    if not isinstance(account_id, str) or not account_id.strip():
        return {"ok": False, "message": "missing Chatgpt-Account-Id", "http_status": None, "error_code": "42211"}

    target_url = "https://chatgpt.com/backend-api/wham/usage"
    headers = {
        "Authorization": f"Bearer {access_token.strip()}",
        "Chatgpt-Account-Id": account_id.strip(),
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
    }

    req = urllib.request.Request(url=target_url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            raw = resp.read()
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else ""
            if not (200 <= code < 300):
                return {"ok": False, "message": f"upstream status {code}", "http_status": code, "error_code": "42299"}
            try:
                payload = json.loads(text) if text else {}
            except Exception:
                return {"ok": False, "message": "quota response parse failed", "http_status": code, "error_code": "42212"}
            return {"ok": True, "message": "quota check passed", "http_status": code, "error_code": "20000", "payload": payload}
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", None)
        if code == 401:
            return {"ok": False, "message": "unauthorized token", "http_status": code, "error_code": "42201"}
        if code in (402, 403):
            return {"ok": False, "message": "permission/subscription denied", "http_status": code, "error_code": "42202"}
        if code == 429:
            return {"ok": False, "message": "rate or quota limited", "http_status": code, "error_code": "42203"}
        if code == 404:
            return {"ok": False, "message": "resource not found", "http_status": code, "error_code": "42204"}
        if code and 500 <= code < 600:
            return {"ok": False, "message": "upstream server error", "http_status": code, "error_code": "42301"}
        return {"ok": False, "message": f"request failed ({code})", "http_status": code, "error_code": "42299"}
    except Exception as e:
        return {"ok": False, "message": f"network error: {e}", "http_status": None, "error_code": "42302"}
def _build_quota_error_result(error_code: str, message: str, http_status=None, account_id: Optional[str] = None, tried_urls: Optional[List[str]] = None):
    return {
        "ok": False,
        "account_id": account_id,
        "http_status": http_status,
        "error_code": error_code,
        "message": message,
        "tried_urls": tried_urls or [],
        "weekly": {
            "ok": False,
            "remaining_percent": None,
            "used_percent": None,
            "reset_after_seconds": None,
            "reset_at": None,
            "error_code": error_code,
            "http_status": http_status,
            "message": message,
        },
        "code_review_weekly": {
            "ok": False,
            "remaining_percent": None,
            "used_percent": None,
            "reset_after_seconds": None,
            "reset_at": None,
            "error_code": error_code,
            "http_status": http_status,
            "message": message,
        },
    }


def _strict_get_codex_quota(token_data: Dict[str, Any]):
    access_token = token_data.get("access_token") or ""
    account_id = _resolve_chatgpt_account_id(token_data)
    usage = _strict_fetch_codex_usage(access_token, account_id or "")
    if not usage.get("ok"):
        return _build_quota_error_result(
            usage.get("error_code") or "42299",
            usage.get("message") or "quota check failed",
            usage.get("http_status"),
            account_id=account_id,
            tried_urls=usage.get("tried_urls") or [],
        )

    payload = usage.get("payload")
    if not isinstance(payload, dict):
        return _build_quota_error_result("42212", "quota response format invalid", usage.get("http_status"), account_id=account_id)

    rate_limit = payload.get("rate_limit") or payload.get("rateLimit") or {}
    code_review = payload.get("code_review_rate_limit") or payload.get("codeReviewRateLimit") or {}

    weekly = _build_quota_card(rate_limit if isinstance(rate_limit, dict) else {}, "weekly quota", "42213", http_status=usage.get("http_status"))
    review_weekly = _build_quota_card(code_review if isinstance(code_review, dict) else {}, "code review weekly quota", "42214", http_status=usage.get("http_status"))

    overall_ok = bool(weekly.get("ok") and review_weekly.get("ok"))
    overall_code = "20000" if overall_ok else (weekly.get("error_code") if not weekly.get("ok") else review_weekly.get("error_code"))
    overall_msg = "quota check passed" if overall_ok else ((weekly.get("message") if not weekly.get("ok") else review_weekly.get("message")) or "quota check failed")

    return {
        "ok": overall_ok,
        "account_id": account_id,
        "http_status": usage.get("http_status"),
        "error_code": overall_code or "42299",
        "message": overall_msg,
        "weekly": weekly,
        "code_review_weekly": review_weekly,
    }
def _strict_cache_get(filename: str, mtime: float):
    with _strict_status_cache_lock:
        row = _strict_status_cache.get(filename)
    if not row:
        return None
    if float(row.get("mtime") or 0) != float(mtime):
        return None
    result = row.get("result")
    return result if isinstance(result, dict) else None


def _strict_cache_set(filename: str, mtime: float, result: dict):
    with _strict_status_cache_lock:
        _strict_status_cache[filename] = {"mtime": float(mtime), "result": dict(result)}


def _get_token_status_for_file(filename: str, strict: bool = False):
    safe_name = os.path.basename(filename)
    path = os.path.join(core._TOKEN_DIR, safe_name)
    if not os.path.isfile(path):
        return {"status": "missing", "message": "token file not found", "expired_at": None, "last_refresh": None, "file": safe_name, "check_mode": "local", "error_code": "42101"}

    file_mtime = os.path.getmtime(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"status": "invalid", "message": "token file invalid", "expired_at": None, "last_refresh": None, "file": safe_name, "check_mode": "local", "error_code": "42102"}

    exp_dt = _parse_iso_datetime(data.get("expired") or data.get("expires_at") or data.get("expiresAt"))
    if exp_dt is None:
        payload = _decode_jwt_payload(data.get("access_token") or "")
        if isinstance(payload, dict) and payload.get("exp"):
            try:
                exp_dt = datetime.fromtimestamp(int(payload.get("exp")), tz=timezone.utc)
            except Exception:
                exp_dt = None

    refresh_dt = _parse_iso_datetime(data.get("last_refresh") or data.get("lastRefresh") or data.get("last_refreshed_at"))

    now = datetime.now(timezone.utc)
    if exp_dt is None:
        status = "unknown"
        msg = "cannot parse expiry"
    elif exp_dt <= now:
        status = "expired"
        msg = "token expired"
    else:
        remain = int((exp_dt - now).total_seconds())
        if remain <= 24 * 3600:
            status = "expiring"
            msg = "token expiring soon"
        else:
            status = "active"
            msg = "token active"

    result = {
        "status": status,
        "message": msg,
        "expired_at": exp_dt.isoformat() if exp_dt else None,
        "last_refresh": refresh_dt.isoformat() if refresh_dt else None,
        "file": safe_name,
        "check_mode": "local",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error_code": "20000" if status in {"active", "expiring"} else ("42103" if status == "expired" else "42199"),
    }

    cached = _strict_cache_get(safe_name, file_mtime)
    if cached:
        cached_checked_at = _parse_iso_datetime(cached.get("checked_at"))
        is_recent_strict = bool(cached_checked_at and (datetime.now(timezone.utc) - cached_checked_at).total_seconds() <= 45)
        if (not strict) or is_recent_strict:
            merged = dict(result)
            merged.update({
                "status": cached.get("status", merged["status"]),
                "message": cached.get("message", merged["message"]),
                "strict_ok": cached.get("strict_ok", False),
                "strict_http_status": cached.get("strict_http_status"),
                "error_code": cached.get("error_code", merged.get("error_code", "42199")),
                "checked_at": cached.get("checked_at", merged["checked_at"]),
                "check_mode": "strict_cached" if not strict else "strict_cached_fast",
                "quota": cached.get("quota"),
            })
            return merged

    if strict:
        try:
            quota = _strict_get_codex_quota(data)
        except Exception as e:
            quota = _build_quota_error_result("42302", f"network error: {e}")

        if quota.get("ok"):
            verify = {
                "ok": True,
                "message": "strict check passed",
                "http_status": quota.get("http_status"),
                "error_code": "20000",
            }
        else:
            quota_http = quota.get("http_status")
            quota_code = str(quota.get("error_code") or "")
            if quota_http in {401, 402, 403, 404, 429} or quota_code in {"42210", "42211", "42201", "42202", "42203", "42204"}:
                verify = {
                    "ok": False,
                    "message": quota.get("message") or "strict check failed",
                    "http_status": quota_http,
                    "error_code": quota.get("error_code") or "42299",
                }
            else:
                try:
                    verify = _strict_verify_access_token(data.get("access_token") or "")
                except Exception as e:
                    verify = {"ok": False, "message": f"network error: {e}", "http_status": None, "error_code": "42302"}

        result["strict_ok"] = bool(verify.get("ok") or quota.get("ok"))
        result["strict_http_status"] = verify.get("http_status")
        result["check_mode"] = "strict"
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        result["error_code"] = verify.get("error_code") or result.get("error_code", "42199")

        if verify.get("ok"):
            result["status"] = "active"
            result["message"] = "strict check passed"
        else:
            code = verify.get("http_status")
            if code == 401:
                result["status"] = "invalid"
            elif code in (402, 403, 404, 429):
                result["status"] = "unknown"
            elif code and 500 <= int(code) < 600:
                result["status"] = "unknown"
            else:
                result["status"] = "unknown" if result["status"] not in {"expired", "missing", "invalid"} else result["status"]
            result["message"] = verify.get("message") or result["message"]

        result["quota"] = quota

        _strict_cache_set(safe_name, file_mtime, {
            "status": result["status"],
            "message": result["message"],
            "strict_ok": result.get("strict_ok", False),
            "strict_http_status": result.get("strict_http_status"),
            "error_code": result.get("error_code", "42199"),
            "checked_at": result["checked_at"],
            "quota": quota,
        })

    return result
def _get_latest_token_filename(email: str, token_index: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Optional[str]:
    if isinstance(token_index, dict):
        files = token_index.get(email, [])
        if files:
            return files[0].get("name")
        return None

    token_dir = core._TOKEN_DIR
    if not os.path.isdir(token_dir):
        return None
    prefix = f"{email}-"
    latest_name = None
    latest_mtime = -1.0
    for name in os.listdir(token_dir):
        if not name.endswith(".json") or not name.startswith(prefix):
            continue
        path = os.path.join(token_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_name = name
    return latest_name


def _get_token_status_for_email(email: str, strict: bool = False, token_index: Optional[Dict[str, List[Dict[str, Any]]]] = None):
    latest = _get_latest_token_filename(email, token_index=token_index)
    if not latest:
        return {"status": "missing", "message": "鏃?token 鏂囦欢", "expired_at": None, "last_refresh": None, "check_mode": "local", "error_code": "42101"}
    return _get_token_status_for_file(latest, strict=strict)

def _delete_tokens_for_email(email: str):
    token_dir = core._TOKEN_DIR
    if not os.path.isdir(token_dir):
        return
    prefix = f"{email}-"
    for name in os.listdir(token_dir):
        if name.endswith(".json") and name.startswith(prefix):
            path = os.path.join(token_dir, name)
            try:
                os.remove(path)
            except Exception as e:
                print(f"Failed to delete {path}: {e}")


def _accounts_summary(rows: List[dict]):
    total = len(rows)
    normal = 0
    abnormal = 0
    for row in rows:
        status = ((row.get("token_status") or {}).get("status") or "unknown").lower()
        if status == "active":
            normal += 1
        if status in {"expired", "invalid", "missing"}:
            abnormal += 1
    return {
        "total_accounts": total,
        "normal_accounts": normal,
        "abnormal_accounts": abnormal,
    }


def _register_one_web(task: TaskState, idx: int, total: int, proxy: Optional[str], output_file: str):
    if task.cancel_event.is_set():
        _push_log(task, f"[TASK] account={idx} cancelled before start")
        return False, None, "task cancelled"

    _push_log(task, f"[TASK] account={idx} worker_boot (delegate=chatgpt_register._register_one)")
    try:
        ok, email, err = core._register_one(idx, total, proxy, output_file)
        return bool(ok), email, err
    except Exception as e:
        _push_log(task, f"[TASK] account={idx} delegate exception: {e}")
        return False, None, str(e)

def _run_task(task: TaskState):
    task.status = "running"
    task.started_at = time.time()

    old_print = getattr(core, "print", print)

    def tee_print(*args, **kwargs):
        msg = " ".join(str(a) for a in args)
        if msg:
            _push_log(task, msg)
        return old_print(*args, **kwargs)

    try:
        core.print = tee_print
        output_file = _resolve_output_file(task.output_file)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        _push_log(task, f"[TASK] start task_id={task.task_id} total={task.total_accounts} workers={task.max_workers}")

        next_idx = 1
        futures = {}
        executor = ThreadPoolExecutor(max_workers=min(task.max_workers, task.total_accounts))
        try:
            while next_idx <= task.total_accounts and len(futures) < task.max_workers and not task.cancel_event.is_set():
                _push_log(task, f"[TASK] account={next_idx} started")
                task.started_count += 1
                f = executor.submit(_register_one_web, task, next_idx, task.total_accounts, task.proxy, output_file)
                futures[f] = next_idx
                next_idx += 1

            while futures:
                if task.cancel_event.is_set():
                    task.status = "stopping"
                    _push_log(task, "[TASK] immediate stop: aborting active workers")
                    _force_abort_active_workers(task)
                    for f in list(futures.keys()):
                        f.cancel()
                    break

                done, _ = wait(futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done:
                    _push_log(
                        task,
                        f"[TASK] live running={len(futures)} started={task.started_count} done={task.completed_count} success={task.success_count} fail={task.fail_count}",
                    )
                    continue

                for future in done:
                    idx = futures.pop(future)
                    try:
                        ok, _email, err = future.result()
                        task.completed_count += 1
                        if ok:
                            task.success_count += 1
                            _push_log(task, f"[TASK] account={idx} success")
                        else:
                            task.fail_count += 1
                            _push_log(task, f"[TASK] account={idx} failed: {err}")
                    except CancelledError:
                        _push_log(task, f"[TASK] account={idx} cancelled")
                    except Exception as ex:
                        task.completed_count += 1
                        task.fail_count += 1
                        _push_log(task, f"[TASK] account={idx} exception: {ex}")

                    _push_log(
                        task,
                        f"[TASK] progress done={task.completed_count}/{task.total_accounts} success={task.success_count} fail={task.fail_count}",
                    )

                    if not task.cancel_event.is_set() and next_idx <= task.total_accounts:
                        _push_log(task, f"[TASK] account={next_idx} started")
                        task.started_count += 1
                        nf = executor.submit(_register_one_web, task, next_idx, task.total_accounts, task.proxy, output_file)
                        futures[nf] = next_idx
                        next_idx += 1

            if task.cancel_event.is_set():
                task.status = "stopped"
            else:
                task.status = "completed"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    except Exception as ex:
        task.status = "failed"
        _push_log(task, f"[TASK] fatal: {ex}")
        _push_log(task, traceback.format_exc())
    finally:
        core.print = old_print
        task.ended_at = time.time()
        _push_log(
            task,
            f"[TASK] end status={task.status} success={task.success_count} fail={task.fail_count} done={task.completed_count}/{task.total_accounts}",
        )


@app.get("/api/auth/session")
def auth_session(request: Request):
    user, _pwd = _get_panel_auth_settings()
    return {"ok": True, "authenticated": _is_authenticated(request), "username": user if _is_authenticated(request) else ""}


@app.post("/api/auth/login")
def auth_login(req: LoginRequest):
    user, pwd = _get_panel_auth_settings()
    if req.username.strip() != user or req.password != pwd:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    resp = JSONResponse({"ok": True, "username": user})
    resp.set_cookie(AUTH_COOKIE_NAME, f"{user}:{pwd}", httponly=True, samesite="Lax", secure=False, max_age=86400*30, path="/")
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return resp


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/auto-maintain")
def get_auto_maintain_status():
    with _auto_maintain_lock:
        return {
            "enabled": _auto_maintain_state.enabled,
            "running": _auto_maintain_state.running,
            "interval_seconds": _auto_maintain_state.interval_seconds,
            "target_count": _auto_maintain_state.target_count,
            "max_workers": _auto_maintain_state.max_workers,
            "remote_valid_count": _auto_maintain_state.remote_valid_count,
            "last_started_at": _auto_maintain_state.last_started_at,
            "last_finished_at": _auto_maintain_state.last_finished_at,
            "last_error": _auto_maintain_state.last_error,
            "logs": _auto_maintain_state.logs[-100:],
        }


@app.get("/api/settings/detect")
def get_detect_settings():
    settings = _get_detect_settings()
    return {
        "detect_base_url": settings.get("detect_base_url") or "",
        "detect_api_key": settings.get("detect_api_key") or "",
    }


@app.put("/api/settings/detect")
def update_detect_settings(req: DetectSettingsRequest):
    settings = _update_detect_settings(req.detect_base_url, req.detect_api_key)
    return {
        "ok": True,
        "detect_base_url": settings.get("detect_base_url") or "",
        "detect_api_key": settings.get("detect_api_key") or "",
    }


@app.post("/api/tasks")
def start_task(req: StartTaskRequest):
    task_id = uuid.uuid4().hex[:12]
    task = TaskState(
        task_id=task_id,
        total_accounts=req.total_accounts,
        max_workers=req.max_workers,
        proxy=req.proxy,
        output_file=req.output_file or "registered_accounts.txt",
    )
    with _task_lock:
        _tasks[task_id] = task

    t = threading.Thread(target=_run_task, args=(task,), daemon=True)
    t.start()
    return {"task_id": task_id, "status": task.status}


@app.post("/api/tasks/{task_id}/stop", response_model=StopTaskResponse)
def stop_task(task_id: str):
    with _task_lock:
        task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status not in {"running", "pending", "stopping"}:
        return StopTaskResponse(task_id=task_id, status=task.status)
    task.cancel_event.set()
    task.status = "stopping"
    _push_log(task, f"[TASK] stop requested task_id={task.task_id}")
    _force_abort_active_workers_async(task)
    return StopTaskResponse(task_id=task_id, status="stopping")


@app.get("/api/tasks")
def list_tasks():
    with _task_lock:
        rows = list(_tasks.values())
    rows.sort(key=lambda x: x.created_at, reverse=True)
    return [
        {
            "task_id": t.task_id,
            "status": t.status,
            "created_at": t.created_at,
            "started_at": t.started_at,
            "ended_at": t.ended_at,
            "total_accounts": t.total_accounts,
            "max_workers": t.max_workers,
            "started_count": t.started_count,
            "success_count": t.success_count,
            "fail_count": t.fail_count,
            "completed_count": t.completed_count,
        }
        for t in rows
    ]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    with _task_lock:
        task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "total_accounts": task.total_accounts,
        "max_workers": task.max_workers,
        "started_count": task.started_count,
        "success_count": task.success_count,
        "fail_count": task.fail_count,
        "completed_count": task.completed_count,
        "logs": task.logs[-300:],
    }


@app.get("/api/accounts")
def list_accounts():
    rows = _read_accounts()
    token_index = _build_token_index()
    status_cache: Dict[str, dict] = {}
    data = [
        {
            "index": row["index"],
            "email": row["email"],
            "account_password": row["account_password"],
            "email_password": row["email_password"],
            "oauth": row["oauth"],
            "token_files": _token_files_for_email(row["email"], token_index=token_index, status_cache=status_cache),
            "token_status": _get_token_status_for_email(row["email"], token_index=token_index),
        }
        for row in rows
    ]
    return {
        "accounts": data,
        "summary": _accounts_summary(data),
    }


@app.put("/api/accounts/{email}")
def update_account(email: str, req: AccountUpsertRequest):
    old_key = _normalize_email(email)
    new_row = {
        "email": req.email.strip(),
        "account_password": req.account_password.strip(),
        "email_password": (req.email_password or "").strip(),
        "oauth": (req.oauth or "").strip(),
    }
    if not new_row["email"] or not new_row["account_password"]:
        raise HTTPException(status_code=400, detail="email and account_password required")

    with _account_file_lock:
        rows = _read_accounts_raw()
        idx = -1
        for i, row in enumerate(rows):
            if _normalize_email(row.get("email", "")) == old_key:
                idx = i
                break
        if idx < 0:
            raise HTTPException(status_code=404, detail="account not found")

        new_key = _normalize_email(new_row["email"])
        for i, row in enumerate(rows):
            if i != idx and _normalize_email(row.get("email", "")) == new_key:
                raise HTTPException(status_code=409, detail="target email already exists")

        rows[idx] = new_row
        _write_accounts_raw(rows)
    return {"ok": True}


@app.delete("/api/accounts/{email}")
def delete_account(email: str):
    key = _normalize_email(email)
    with _account_file_lock:
        rows = _read_accounts_raw()
        new_rows = [r for r in rows if _normalize_email(r.get("email", "")) != key]
        if len(new_rows) == len(rows):
            raise HTTPException(status_code=404, detail="account not found")
        _write_accounts_raw(new_rows)
        _delete_tokens_for_email(email)
    return {"ok": True}
    return {"ok": True}


@app.post("/api/accounts/batch-delete")
def batch_delete_accounts(req: AccountBatchDeleteRequest):
    keys = {_normalize_email(x) for x in req.emails if (x or "").strip()}
    if not keys:
        raise HTTPException(status_code=400, detail="emails required")
    with _account_file_lock:
        rows = _read_accounts_raw()
        before = len(rows)
        new_rows = [r for r in rows if _normalize_email(r.get("email", "")) not in keys]
        deleted = before - len(new_rows)
        if deleted <= 0:
            raise HTTPException(status_code=404, detail="no matched accounts")
        _write_accounts_raw(new_rows)
        for key in keys:
            _delete_tokens_for_email(key)
    return {"ok": True, "deleted": deleted}
    return {"ok": True, "deleted": deleted}


@app.delete("/api/accounts")
def clear_all_accounts():
    with _account_file_lock:
        rows = _read_accounts_raw()
        _write_accounts_raw([])
        for r in rows:
            _delete_tokens_for_email(r.get("email", ""))
    return {"ok": True}


@app.post("/api/accounts/export")
def export_accounts(req: ExportAccountsRequest):
    rows = _read_accounts()
    if not rows:
        raise HTTPException(status_code=404, detail="no accounts to export")

    limit = min(max(1, int(req.count or 1)), len(rows))
    picked = rows[:limit]
    text = "\n".join([f"{(x.get('email') or '').strip()}----{(x.get('account_password') or '').strip()}" for x in picked]) + "\n"

    filename = f"accounts_export_{limit}.txt"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return Response(content=text, media_type="text/plain; charset=utf-8", headers=headers)


@app.post("/api/accounts/clear-abnormal")
def clear_abnormal_accounts():
    with _account_file_lock:
        rows = _read_accounts_raw()
        kept = []
        deleted = 0
        for row in rows:
            status = (_get_token_status_for_email(row.get("email", "")).get("status") or "unknown").lower()
            if status in {"expired", "invalid", "missing"}:
                deleted += 1
                _delete_tokens_for_email(row.get("email", ""))
                continue
            kept.append(row)
        if deleted <= 0:
            return {"ok": True, "deleted": 0}
        _write_accounts_raw(kept)
    return {"ok": True, "deleted": deleted}


@app.get("/api/accounts/{email}/tokens")
def list_account_tokens(email: str):
    return _token_files_for_email(email)


@app.get("/api/tokens/{filename}")
def get_token_file(filename: str):
    return _read_token_json(filename)


@app.post("/api/tokens/{filename}/check")
def check_token_file(filename: str):
    return _get_token_status_for_file(filename, strict=True)


@app.post("/api/accounts/{email}/check")
def check_account_tokens(email: str):
    token_index = _build_token_index()
    status = _get_token_status_for_email(email, strict=True, token_index=token_index)
    return {"email": email, "token_status": status}


@app.post("/api/accounts/check-all")
def check_all_account_tokens():
    rows = _read_accounts()
    if not rows:
        return {"results": []}

    token_index = _build_token_index()
    worker_count = min(max(1, len(rows)), 20)
    data = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_get_token_status_for_email, row["email"], True, token_index): row["email"]
            for row in rows
        }
        for future in as_completed(futures):
            email = futures[future]
            try:
                status = future.result()
            except Exception as e:
                status = {
                    "status": "unknown",
                    "message": f"妫€娴嬪け璐? {e}",
                    "expired_at": None,
                    "last_refresh": None,
                    "check_mode": "strict",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "error_code": "42302",
                    "strict_ok": False,
                    "strict_http_status": None,
                    "quota": _build_quota_error_result("42302", f"妫€娴嬪け璐? {e}"),
                }
            data.append({"email": email, "token_status": status})

    return {"results": data}


@app.post("/api/codex-push/check")
def check_codex_push_target(req: CodexProxyCheckRequest):
    base_url, api_key = _get_cliproxy_management_settings(req.api_base_url, req.api_key)
    if not base_url:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_BASE_URL")
    if not api_key:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_KEY")

    local_files = _list_local_codex_token_files()
    local_names = {x.get("name") for x in local_files if x.get("name")}

    remote_files = []
    remote_error = ""
    try:
        remote_files = _get_remote_auth_files(base_url, api_key)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (e.read() or b"").decode("utf-8", errors="ignore")
        except Exception:
            detail = ""
        remote_error = f"绠＄悊棰勬澶辫触: HTTP {getattr(e, 'code', None)} {detail[:200]}"
    except Exception as e:
        remote_error = f"绠＄悊棰勬澶辫触: {e}"

    remote_codex_names = set()
    for item in remote_files:
        if not isinstance(item, dict):
            continue
        provider_type = str(item.get("type") or item.get("provider") or "").strip().lower()
        if provider_type != "codex":
            continue
        name = str(item.get("name") or item.get("file_name") or item.get("filename") or "").strip()
        if name:
            remote_codex_names.add(name)

    overlap = sorted([x for x in local_names if x in remote_codex_names])

    return {
        "ok": remote_error == "",
        "remote_error": remote_error,
        "local_total": len(local_files),
        "remote_codex_total": len(remote_codex_names),
        "remote_codex_names": sorted(list(remote_codex_names)),
        "remote_overlap_total": len(overlap),
        "remote_overlap_names": overlap,
        "local_files": [
            {
                "name": x.get("name"),
                "size": x.get("size"),
                "mtime": x.get("mtime"),
                "email": x.get("email"),
            }
            for x in local_files
        ],
    }


@app.post("/api/codex-push/single")
def push_single_codex_token(req: PushCodexTokenSingleRequest):
    base_url, api_key = _get_cliproxy_management_settings(req.api_base_url, req.api_key)
    filename = os.path.basename(req.filename or "")
    if not base_url:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_BASE_URL")
    if not api_key:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_KEY")
    if not filename or filename != req.filename:
        raise HTTPException(status_code=400, detail="invalid filename")

    row = _get_local_codex_token_file_by_name(filename)
    if not row:
        raise HTTPException(status_code=404, detail="local codex token file not found")

    path = row.get("path")
    try:
        with open(path, "rb") as f:
            content = f.read()
        code, text = _upload_codex_token_file_to_cliproxy(base_url, api_key, filename, content)
        if not (200 <= int(code) < 300):
            raise HTTPException(status_code=400, detail=f"upload failed: {code} {text[:200]}")

        deleted = False
        if req.delete_local_after_upload:
            try:
                os.remove(path)
                deleted = True
            except Exception as ex:
                raise HTTPException(status_code=400, detail=f"upload ok but delete failed: {ex}")

        return {"ok": True, "file": filename, "uploaded": True, "deleted": deleted}
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (e.read() or b"").decode("utf-8", errors="ignore")
        except Exception:
            detail = ""
        raise HTTPException(status_code=400, detail=f"http error: {getattr(e, 'code', None)} {detail[:200]}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class RemoteFileCheckResult(BaseModel):
    name: str
    is_invalid: bool
    status_code: Optional[int]
    message: str

class CheckRemoteStatusRequest(BaseModel):
    api_base_url: Optional[str] = ""
    api_key: Optional[str] = ""

class CheckRemoteStatusSingleRequest(BaseModel):
    api_base_url: Optional[str] = ""
    api_key: Optional[str] = ""
    filename: str

class CheckRemoteStatusBatchRequest(BaseModel):
    api_base_url: Optional[str] = ""
    api_key: Optional[str] = ""
    filenames: List[str] = Field(default_factory=list)
    max_workers: int = Field(default=16, ge=1, le=64)

class DeleteRemoteFilesRequest(BaseModel):
    api_base_url: Optional[str] = ""
    api_key: Optional[str] = ""
    filenames: List[str]


def _check_one_remote_codex_file(base_url: str, api_key: str, name: str, item_hint: Optional[Dict[str, Any]] = None):
    import urllib.parse

    safe_name = str(name or "").strip()
    if not safe_name:
        return {"name": "", "is_invalid": True, "status_code": None, "message": "invalid filename"}

    def _get_remote_file_content(filename: str):
        target_url = _join_base_url(base_url, f"/v0/management/auth-files/download?name={urllib.parse.quote(filename)}")
        get_req = urllib.request.Request(url=target_url, method="GET", headers=_management_headers(api_key))
        try:
            with urllib.request.urlopen(get_req, timeout=10) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    raw = resp.read() or b""
                    return json.loads(raw.decode("utf-8", errors="ignore"))
                return None
        except Exception:
            return None

    item = item_hint if isinstance(item_hint, dict) else {}
    token_fields = _extract_remote_token_fields(item)
    access_token = token_fields.get("access_token") or ""
    if not access_token:
        full_item = _get_remote_file_content(safe_name)
        if isinstance(full_item, dict):
            item = full_item
            token_fields = _extract_remote_token_fields(full_item)
            access_token = token_fields.get("access_token") or ""

    is_invalid = False
    status_code = None
    message = "normal"

    if access_token:
        token_payload = dict(item) if isinstance(item, dict) else {}
        token_payload["access_token"] = access_token
        if token_fields.get("id_token"):
            token_payload["id_token"] = token_fields["id_token"]
        if token_fields.get("refresh_token"):
            token_payload["refresh_token"] = token_fields["refresh_token"]
        if token_fields.get("account_id"):
            token_payload["account_id"] = token_fields["account_id"]

        quota = _strict_get_codex_quota(token_payload)
        if quota.get("ok"):
            weekly = ((quota.get("weekly") or {}) if isinstance(quota, dict) else {})
            remaining = weekly.get("remaining_percent")
            status_code = quota.get("http_status")
            if remaining is not None and float(remaining) <= 0:
                is_invalid = True
                message = "quota exhausted (remaining=0)"
            else:
                message = "quota ok"
        else:
            quota_http = quota.get("http_status")
            quota_code = str(quota.get("error_code") or "")
            if (quota_http in {401, 402, 403, 404, 429} or quota_code in {"42210", "42201", "42202", "42203", "42204"}) and quota_code != "42211":
                status_code = quota_http
                message = quota.get("message") or "check failed"
                if status_code == 401:
                    is_invalid = True
                    message = "unauthorized (401)"
            else:
                verify_res = _strict_verify_access_token(access_token, timeout=4.0)
                status_code = verify_res.get("http_status")
                if status_code == 401:
                    is_invalid = True
                    message = "token unauthorized (401)"
                elif verify_res.get("ok"):
                    message = "token verify ok"
                else:
                    message = verify_res.get("message") or f"unknown error (HTTP {status_code})"
    else:
        is_invalid = True
        message = "no access_token (remote file has no token field)"

    return {
        "name": safe_name,
        "is_invalid": is_invalid,
        "status_code": status_code,
        "message": message,
    }


@app.post("/api/codex-push/check-remote-status")
def check_remote_status(req: CheckRemoteStatusRequest):
    base_url, api_key = _get_cliproxy_management_settings(req.api_base_url, req.api_key)
    if not base_url:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_BASE_URL")
    if not api_key:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_KEY")

    try:
        remote_files = _get_remote_auth_files(base_url, api_key)
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
        raise HTTPException(status_code=400, detail=f"management precheck failed: HTTP {getattr(e, 'code', None)} {detail[:200]}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"management precheck failed: {e}")

    candidates: List[Dict[str, Any]] = []
    for item in remote_files:
        if not isinstance(item, dict):
            continue
        provider_type = str(item.get("type") or item.get("provider") or "").strip().lower()
        if provider_type != "codex":
            continue
        name = str(item.get("name") or item.get("file_name") or item.get("filename") or "").strip()
        if not name:
            continue
        candidates.append({"name": name, "item": item})

    results: List[Dict[str, Any]] = []
    if candidates:
        worker_count = min(max(1, len(candidates)), 12)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(_check_one_remote_codex_file, base_url, api_key, c["name"], c["item"]): idx
                for idx, c in enumerate(candidates)
            }
            ordered: Dict[int, Dict[str, Any]] = {}
            for future in as_completed(future_map):
                idx = future_map[future]
                name = str(candidates[idx].get("name") or "")
                try:
                    ordered[idx] = future.result()
                except Exception as ex:
                    ordered[idx] = {
                        "name": name,
                        "is_invalid": True,
                        "status_code": None,
                        "message": f"check failed: {ex}",
                    }
        for idx in range(len(candidates)):
            if idx in ordered:
                results.append(ordered[idx])

    return {
        "ok": True,
        "results": results,
    }


@app.post("/api/codex-push/check-remote-status-single")
def check_remote_status_single(req: CheckRemoteStatusSingleRequest):
    base_url, api_key = _get_cliproxy_management_settings(req.api_base_url, req.api_key)
    filename = os.path.basename(req.filename or "")
    if not base_url:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_BASE_URL")
    if not api_key:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_KEY")
    if not filename or filename != req.filename:
        raise HTTPException(status_code=400, detail="invalid filename")

    result = _check_one_remote_codex_file(base_url, api_key, filename)
    return {"ok": True, "result": result}


@app.post("/api/codex-push/check-remote-status-batch")
def check_remote_status_batch(req: CheckRemoteStatusBatchRequest):
    base_url, api_key = _get_cliproxy_management_settings(req.api_base_url, req.api_key)
    if not base_url:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_BASE_URL")
    if not api_key:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_KEY")

    safe_names: List[str] = []
    for n in req.filenames or []:
        name = os.path.basename(n or "")
        if not name or name != n:
            continue
        safe_names.append(name)

    if not safe_names:
        return {"ok": True, "results": []}

    worker_count = min(max(1, int(req.max_workers or 1)), len(safe_names))
    ordered: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_check_one_remote_codex_file, base_url, api_key, name): idx
            for idx, name in enumerate(safe_names)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            name = safe_names[idx]
            try:
                ordered[idx] = future.result()
            except Exception as ex:
                ordered[idx] = {
                    "name": name,
                    "is_invalid": True,
                    "status_code": None,
                    "message": f"check failed: {ex}",
                }

    results = [ordered[i] for i in range(len(safe_names)) if i in ordered]
    return {"ok": True, "results": results}
@app.post("/api/codex-push/delete-remote-files")
def delete_remote_files(req: DeleteRemoteFilesRequest):
    base_url, api_key = _get_cliproxy_management_settings(req.api_base_url, req.api_key)
    if not base_url or not api_key:
        raise HTTPException(status_code=400, detail="missing CLIPROXY_API_BASE_URL or CLIPROXY_API_KEY")
    if not req.filenames:
        return {"ok": True, "deleted": [], "failed": []}

    import urllib.parse
    deleted = []
    failed = []

    def _delete_remote_file(name: str):
        target_url = _join_base_url(base_url, f"/v0/management/auth-files?name={urllib.parse.quote(name)}")
        del_req = urllib.request.Request(url=target_url, method="DELETE", headers=_management_headers(api_key))
        try:
            with urllib.request.urlopen(del_req, timeout=10) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True, ""
                return False, f"Status: {getattr(resp, 'status', None)}"
        except urllib.error.HTTPError as e:
            detail = (e.read() or b"").decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
            return False, f"HTTP {e.code}: {detail[:100]}"
        except Exception as ex:
            return False, str(ex)

    for name in req.filenames:
        ok, err = _delete_remote_file(name)
        if ok:
            deleted.append(name)
        else:
            failed.append({"name": name, "error": err})

    return {
        "ok": True,
        "deleted": deleted,
        "failed": failed
    }
@app.websocket("/ws/tasks/{task_id}/logs")
async def ws_logs(websocket: WebSocket, task_id: str):
    await websocket.accept()

    with _task_lock:
        task = _tasks.get(task_id)
    if not task:
        await websocket.send_json({"type": "error", "message": "task not found"})
        await websocket.close()
        return

    for line in task.logs[-300:]:
        await websocket.send_json(
            {
                "type": "log",
                "line": line,
                "status": task.status,
                "started": task.started_count,
                "success": task.success_count,
                "fail": task.fail_count,
                "done": task.completed_count,
                "total": task.total_accounts,
            }
        )

    try:
        while True:
            if task.status in {"completed", "failed", "stopped"}:
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "status": task.status,
                        "started": task.started_count,
                        "success": task.success_count,
                        "fail": task.fail_count,
                        "done": task.completed_count,
                        "total": task.total_accounts,
                    }
                )
                await websocket.send_json({"type": "done", "status": task.status})
                break
            try:
                line = task.log_queue.get(timeout=1)
                await websocket.send_json(
                    {
                        "type": "log",
                        "line": line,
                        "status": task.status,
                        "started": task.started_count,
                        "success": task.success_count,
                        "fail": task.fail_count,
                        "done": task.completed_count,
                        "total": task.total_accounts,
                    }
                )
            except Empty:
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "status": task.status,
                        "started": task.started_count,
                        "success": task.success_count,
                        "fail": task.fail_count,
                        "done": task.completed_count,
                        "total": task.total_accounts,
                    }
                )
    except WebSocketDisconnect:
        return


FRONTEND_DIST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "dist"))
if os.path.isdir(FRONTEND_DIST_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST_DIR, html=True), name="frontend")


