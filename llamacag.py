#!/usr/bin/env python3
"""llama-cag-n8n management CLI. Python 3.10+ standard library only.

    python llamacag.py setup            create .env with generated secrets
    python llamacag.py start [--gpu]    bring the stack up
    python llamacag.py start --native-llama  stack in Docker, llama-server on host
    python llamacag.py stop             bring the stack down
    python llamacag.py status           container + HTTP health overview
    python llamacag.py logs [service]   tail service logs
    python llamacag.py query "..."      ask a question (test helper)
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
COMPOSE_FILES = ["docker-compose.yml"]
GPU_COMPOSE_FILE = "docker-compose.gpu.yml"
VULKAN_COMPOSE_FILE = "docker-compose.vulkan.yml"

SECRET_KEYS = ("DB_PASSWORD", "N8N_ENCRYPTION_KEY", "N8N_USER_MANAGEMENT_JWT_SECRET")

# The llama-server service sits behind this compose profile so it can be turned
# off for native mode (host-run llama-server, e.g. Apple Silicon / Metal).
LLAMA_PROFILE = "local-llama"
# Where a host-run llama-server should persist its KV slot caches in native
# mode. Unlike Docker mode these live on the host, not in the kv_caches volume.
NATIVE_SLOT_SAVE_PATH = "./kv_caches"

MODEL_DEFAULT = "google/gemma-4-12B-it-qat-q4_0-gguf"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def port(env: dict[str, str], key: str, default: str) -> str:
    return env.get(key, default) or default


def compose_env(profiles: str | None = None) -> dict[str, str]:
    """Subprocess environment for docker compose, resolving COMPOSE_PROFILES.

    `profiles` set to "" or a value forces COMPOSE_PROFILES for this invocation
    (native mode passes ""). Left as None, we fall back to the backward-compat
    default: if neither the shell env nor .env defines COMPOSE_PROFILES, inject
    "local-llama" so old .env files (written before that knob existed) still get
    the all-in-Docker service set.
    """
    env = dict(os.environ)
    if profiles is not None:
        env["COMPOSE_PROFILES"] = profiles
    elif "COMPOSE_PROFILES" not in env and "COMPOSE_PROFILES" not in read_env():
        env["COMPOSE_PROFILES"] = LLAMA_PROFILE
    return env


def run_compose(
    args: list[str],
    *,
    gpu: bool = False,
    vulkan: bool = False,
    profiles: str | None = None,
) -> int:
    files = list(COMPOSE_FILES)
    if gpu:
        files.append(GPU_COMPOSE_FILE)
    if vulkan:
        files.append(VULKAN_COMPOSE_FILE)
    command = ["docker", "compose"]
    for name in files:
        command += ["-f", str(PROJECT_ROOT / name)]
    command += args
    return subprocess.run(command, cwd=PROJECT_ROOT, env=compose_env(profiles)).returncode


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        print("[!!] docker not found on PATH. Install Docker Desktop first.")
        return False
    probe = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, cwd=PROJECT_ROOT
    )
    if probe.returncode != 0:
        print("[!!] Docker is installed but not running. Start Docker Desktop and retry.")
        return False
    return True


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """Returns (status, body); non-2xx responses are read, not raised."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# --- commands ----------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    if ENV_FILE.exists() and not args.force:
        print(f"[OK] .env already exists at {ENV_FILE} (use --force to regenerate secrets)")
    else:
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        for key in SECRET_KEYS:
            token = secrets.token_hex(24)
            content = re.sub(rf"^{key}=.*$", f"{key}={token}", content, flags=re.MULTILINE)
        ENV_FILE.write_text(content, encoding="utf-8")
        print(f"[OK] Wrote {ENV_FILE} with generated secrets")

    documents = PROJECT_ROOT / read_env().get("DOCUMENTS_FOLDER", "./documents")
    documents.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Documents watch folder: {documents.resolve()}")
    print()
    print("Next: python llamacag.py start")
    return 0


def native_llama_command(env: dict[str, str]) -> str:
    """The host llama-server command for native mode, with .env values baked in.

    Mirrors the container command in docker-compose.yml, except the caches live
    on the host (--slot-save-path points at a host folder) and --host binds all
    interfaces so the Dockerised cag-api can reach it via host.docker.internal.
    """
    model = env.get("LLAMA_MODEL", MODEL_DEFAULT)
    ctx = port(env, "LLAMA_CTX_SIZE", "65536")
    slots = port(env, "CAG_SLOTS", "1")
    kv = port(env, "LLAMA_CACHE_TYPE_KV", "q8_0")
    llama_port = port(env, "LLAMA_PORT", "8080")
    extra = env.get("LLAMA_EXTRA_ARGS", "").strip()
    lines = [
        "llama-server \\",
        f"  -hf {model} \\",
        f"  --ctx-size {ctx} \\",
        f"  --parallel {slots} \\",
        f"  --cache-type-k {kv} \\",
        f"  --cache-type-v {kv} \\",
        f"  --slot-save-path {NATIVE_SLOT_SAVE_PATH} \\",
        "  --host 0.0.0.0 \\",
        f"  --port {llama_port}",
    ]
    if extra:
        lines[-1] += " \\"
        lines.append(f"  {extra}")
    return "\n".join(lines)


def print_native_llama_instructions(env: dict[str, str]) -> None:
    save_path = (PROJECT_ROOT / NATIVE_SLOT_SAVE_PATH).resolve()
    print()
    print("=" * 72)
    print("NATIVE llama-server mode — start the model yourself on the host")
    print("=" * 72)
    print("The rest of the stack (cag-api, n8n, Postgres) is running in Docker,")
    print("but llama-server is NOT — Docker has no GPU/Metal passthrough on macOS,")
    print("so on Apple Silicon you run it natively for Metal acceleration.")
    print()
    print("1. Install llama.cpp once (Apple Silicon builds with Metal by default):")
    print("     brew install llama.cpp")
    print()
    print("2. Run this in a SEPARATE terminal and leave it running (the model")
    print("   downloads from Hugging Face on first launch, then serves offline):")
    print()
    print(native_llama_command(env))
    print()
    print(f"   KV slot caches persist on the HOST at {save_path}")
    print("   (native mode does NOT use the Docker kv_caches volume). This folder")
    print("   is created for you; keep it to survive restarts.")
    print()
    print("3. Point cag-api at the host process — make sure .env has:")
    print("     COMPOSE_PROFILES=")
    print("     LLAMA_SERVER_URL=http://host.docker.internal:8080")
    print("   (LLAMA_PORT above must match the :8080 here.)")
    print("=" * 72)


def cmd_start(args: argparse.Namespace) -> int:
    if not ENV_FILE.exists():
        print("[!!] No .env file. Run: python llamacag.py setup")
        return 1
    env = read_env()
    if any(env.get(key, "CHANGE_ME") == "CHANGE_ME" for key in SECRET_KEYS):
        print("[!!] .env still contains CHANGE_ME secrets. Run: python llamacag.py setup --force")
        return 1
    if args.gpu and args.vulkan:
        print("[!!] Pick one of --gpu (NVIDIA/CUDA) or --vulkan (Intel/AMD), not both.")
        return 1
    if args.native_llama and (args.gpu or args.vulkan):
        print("[!!] --native-llama runs llama-server on the host; drop --gpu/--vulkan "
              "(those configure the in-Docker llama-server, which native mode disables).")
        return 1
    if not docker_ready():
        return 1

    if args.native_llama:
        # Native mode: disable the in-Docker llama-server (empty profile) and
        # bring up only the supporting services; the user runs llama-server
        # themselves. Caches live on the host, so ensure the folder exists.
        (PROJECT_ROOT / NATIVE_SLOT_SAVE_PATH).mkdir(parents=True, exist_ok=True)
        code = run_compose(["up", "-d", "--build"], profiles="")
        if code != 0:
            return code
        print_native_llama_instructions(env)
    else:
        # Default all-in-Docker mode. profiles=None lets run_compose inject
        # local-llama when .env predates the COMPOSE_PROFILES knob.
        code = run_compose(["up", "-d", "--build"])
        if code != 0:
            return code

    n8n = f"http://localhost:{port(env, 'N8N_PORT', '5678')}"
    api = f"http://localhost:{port(env, 'CAG_API_PORT', '8000')}"
    model = env.get("LLAMA_MODEL", MODEL_DEFAULT)
    print()
    print("=" * 72)
    print("llama-cag-n8n is starting")
    print("=" * 72)
    print(f"  n8n:       {n8n}   (import the workflows from n8n/workflows/)")
    print(f"  cag-api:   {api}   ({api}/docs for the API browser)")
    print(f"  model:     {model}")
    if not args.native_llama:
        print()
        print("First boot downloads the model (~6.5 GB for the default) before")
        print("llama-server accepts requests. Watch it with:")
        print("  python llamacag.py logs llama-server")
    print("Then check everything with:")
    print("  python llamacag.py status")
    print("=" * 72)
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    if not docker_ready():
        return 1
    return run_compose(["down"])


def cmd_status(_: argparse.Namespace) -> int:
    if not docker_ready():
        return 1
    run_compose(["ps"])
    env = read_env()
    checks = [
        ("n8n", f"http://localhost:{port(env, 'N8N_PORT', '5678')}/healthz"),
        ("cag-api", f"http://localhost:{port(env, 'CAG_API_PORT', '8000')}/health"),
        ("llama-server", f"http://localhost:{port(env, 'LLAMA_PORT', '8080')}/health"),
    ]
    print()
    for name, url in checks:
        try:
            status, body = http_get(url)
        except OSError as exc:
            print(f"[!!] {name:<13} unreachable ({url}): {exc}")
            continue
        marker = "[OK]" if status == 200 else "[!!]"
        detail = ""
        if name == "cag-api":
            try:
                report = json.loads(body)
                detail = f" status={report.get('status')}"
                if isinstance(report.get("llama_server"), dict) and \
                        "error" in report["llama_server"]:
                    detail += " (llama-server not ready — model still loading?)"
            except json.JSONDecodeError:
                pass
        print(f"{marker} {name:<13} HTTP {status} {url}{detail}")

    print_resource_snapshot()
    return 0


def print_resource_snapshot() -> None:
    """Live per-service CPU/RAM so memory pressure is never a mystery.

    The big constant is llama-server: model weights + the KV pool, both
    allocated at startup and flat afterwards (documents cost disk, not RAM).
    llama-server's own startup log prints the exact weights/KV allocation."""
    probe = subprocess.run(
        [
            "docker", "stats", "--no-stream",
            "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
        ],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    rows = [
        line.split("\t")
        for line in probe.stdout.splitlines()
        if line.startswith("llamacag-")
    ]
    if probe.returncode != 0 or not rows:
        return
    print()
    print("Resources (live):")
    print(f"  {'service':<18} {'cpu':>8}   memory")
    for name, cpu, mem in sorted(rows):
        print(f"  {name:<18} {cpu:>8}   {mem}")
    print("  (llama-server RAM = model weights + fixed KV pool, allocated at")
    print("   startup and flat afterwards; documents cost disk, not RAM.")
    print("   Exact allocation breakdown: python llamacag.py logs llama-server)")


def cmd_logs(args: argparse.Namespace) -> int:
    if not docker_ready():
        return 1
    command = ["logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("-f")
    if args.service:
        command.append(args.service)
    return run_compose(command)


def cmd_query(args: argparse.Namespace) -> int:
    env = read_env()
    url = f"http://localhost:{port(env, 'CAG_API_PORT', '8000')}/query"
    payload: dict = {"question": args.question}
    if args.doc is not None:
        payload["document_id"] = args.doc
    if args.max_tokens is not None:
        payload["max_tokens"] = args.max_tokens

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"Asking {url} (CPU inference can take a while)...")
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"[!!] HTTP {exc.code}: {detail}")
        return 1
    except OSError as exc:
        print(f"[!!] cag-api unreachable: {exc}")
        return 1

    print()
    print(body.get("answer", "").strip())
    print()
    doc = body.get("document", {})
    timings = body.get("timings", {})
    print(
        f"-- document #{doc.get('id')} {doc.get('file_name')} | "
        f"cache: {timings.get('cache_source')} | "
        f"evaluated {timings.get('prompt_tokens_evaluated')} prompt tokens, "
        f"{timings.get('prompt_tokens_from_cache')} from cache | "
        f"{body.get('duration_ms')} ms"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="llamacag", description="Manage the llama-cag-n8n stack"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="create .env with generated secrets")
    p_setup.add_argument("--force", action="store_true", help="regenerate .env secrets")
    p_setup.set_defaults(func=cmd_setup)

    p_start = sub.add_parser("start", help="build and start all services")
    p_start.add_argument("--gpu", action="store_true", help="NVIDIA GPU (CUDA image)")
    p_start.add_argument(
        "--vulkan", action="store_true",
        help="Intel/AMD GPU via Vulkan (Linux hosts; see docker-compose.vulkan.yml)",
    )
    p_start.add_argument(
        "--native-llama", action="store_true",
        help="run llama-server on the host (Apple Silicon/Metal); prints the command",
    )
    p_start.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop all services").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="container and health overview").set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="show service logs")
    p_logs.add_argument("service", nargs="?", help="one of: llama-server cag-api n8n db")
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.add_argument("--tail", type=int, default=100)
    p_logs.set_defaults(func=cmd_logs)

    p_query = sub.add_parser("query", help="ask a question against the cached documents")
    p_query.add_argument("question")
    p_query.add_argument("--doc", type=int, help="document id (default: latest cached)")
    p_query.add_argument("--max-tokens", type=int)
    p_query.set_defaults(func=cmd_query)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
