import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.abspath(os.path.dirname(__file__))
HOST = "127.0.0.1"


def npm_executable():
    npm_cmd = shutil.which("npm")
    if npm_cmd:
        return npm_cmd
    candidate = os.path.join(os.path.dirname(sys.executable), "Scripts", "npm.cmd")
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError("npm 未找到，请先安装 Node.js 并确保 npm 在 PATH 中")


def backend_cmd(port: int):
    return [sys.executable, "-m", "uvicorn", "webui.backend.app:app", "--host", HOST, "--port", str(port)]


def frontend_cmd(port: int):
    npm_cmd = npm_executable()
    return [
        npm_cmd,
        "--prefix",
        os.path.join(ROOT, "webui", "frontend"),
        "run",
        "dev",
        "--",
        "--host",
        HOST,
        "--port",
        str(port),
    ]


def can_bind_port(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_available_port(start_port: int, scan: int = 80) -> int:
    for p in range(start_port, start_port + scan):
        if can_bind_port(p):
            return p
    raise RuntimeError(f"找不到可用端口，起始端口={start_port}")


def ensure_frontend_deps():
    node_modules = os.path.join(ROOT, "webui", "frontend", "node_modules")
    if os.path.isdir(node_modules):
        return
    npm_cmd = npm_executable()
    print("[setup] installing frontend dependencies...")
    subprocess.check_call([npm_cmd, "install", "--prefix", os.path.join(ROOT, "webui", "frontend")])


def start_process(cmd, name, env=None):
    print(f"[start] {name}: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=ROOT, env=env)


def wait_backend_ready(port: int, proc, timeout: float = 20):
    deadline = time.time() + timeout
    url = f"http://{HOST}:{port}/api/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"backend process exited (code={proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1.2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("backend health check timeout")


def wait_frontend_ready(port: int, proc, timeout: float = 20):
    deadline = time.time() + timeout
    url = f"http://{HOST}:{port}/"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"frontend process exited (code={proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1.2) as resp:
                if resp.status in (200, 304):
                    return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("frontend health check timeout")


def stop_process(proc, name):
    if proc and proc.poll() is None:
        print(f"[stop] {name}")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    args = parser.parse_args()

    ensure_frontend_deps()

    backend_port = find_available_port(args.backend_port)
    frontend_port = find_available_port(args.frontend_port)

    if backend_port != args.backend_port:
        print(f"[info] backend端口 {args.backend_port} 不可用，自动切换到 {backend_port}")
    if frontend_port != args.frontend_port:
        print(f"[info] frontend端口 {args.frontend_port} 不可用，自动切换到 {frontend_port}")

    backend = None
    frontend = None
    try:
        backend = start_process(backend_cmd(backend_port), "backend")
        wait_backend_ready(backend_port, backend)

        env = os.environ.copy()
        env["VITE_API_BASE"] = f"http://{HOST}:{backend_port}"
        frontend = start_process(frontend_cmd(frontend_port), "frontend", env=env)
        wait_frontend_ready(frontend_port, frontend)

        print(f"\n[ready] backend:  http://{HOST}:{backend_port}")
        print(f"[ready] frontend: http://{HOST}:{frontend_port}")
        print("[tip] Press Ctrl+C to stop both services.\n")

        if not args.no_browser:
            webbrowser.open(f"http://{HOST}:{frontend_port}")

        while True:
            if backend.poll() is not None:
                raise RuntimeError("backend process exited")
            if frontend.poll() is not None:
                raise RuntimeError("frontend process exited")
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        stop_process(frontend, "frontend")
        stop_process(backend, "backend")


if __name__ == "__main__":
    if os.name == "nt":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()
