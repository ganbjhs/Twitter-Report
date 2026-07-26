"""Keep the X session alive across restarts on a host with no disk.

Render's free tier has an ephemeral filesystem and sleeps after ~15 minutes
idle. Without somewhere durable to keep `x_state.json`, every cold start would
sign in to X again — and on a metered browser service each of those sign-ins
eats a minute or two out of a small monthly allowance. So the cookie is mirrored
to a small external store and read back on boot.

Backends (`X_STATE_STORE`):
    none    (default) local file only — right for a host with a real disk
    github  a file in a PRIVATE GitHub repo, via the Contents API

Only the session cookie is stored, never app credentials. Use a repo that is
private and a token scoped to just that repo: whoever holds this file can post
as the capture account.
"""
import base64
import json
import os
import urllib.error
import urllib.request

from . import config

_TIMEOUT = 20


def backend() -> str:
    return (os.environ.get("X_STATE_STORE", "") or "none").strip().lower()


def enabled() -> bool:
    return backend() == "github"


def describe() -> str:
    if backend() == "github":
        return f"GitHub repo {os.environ.get('X_STATE_GITHUB_REPO', '?')}"
    return "local file only"


def _request(url: str, method: str = "GET", headers=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


# --------------------------------------------------------------------------- #
# GitHub (a file in a private repo)
# --------------------------------------------------------------------------- #
def _gh_config():
    repo = (os.environ.get("X_STATE_GITHUB_REPO", "") or "").strip()
    token = (os.environ.get("X_STATE_GITHUB_TOKEN", "") or "").strip()
    path = (os.environ.get("X_STATE_GITHUB_PATH", "") or "x_state.json").strip()
    branch = (os.environ.get("X_STATE_GITHUB_BRANCH", "") or "main").strip()
    return repo, token, path, branch


def _gh_headers(token):
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "report-automation",
            "Content-Type": "application/json"}


def _gh_load():
    repo, token, path, branch = _gh_config()
    if not (repo and token):
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        data = _request(url, headers=_gh_headers(token))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                     # nothing stored yet — normal
        raise
    content = data.get("content") or ""
    return json.loads(base64.b64decode(content).decode("utf-8"))


def _gh_save(state: dict) -> bool:
    repo, token, path, branch = _gh_config()
    if not (repo and token):
        return False
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    sha = None
    try:                                     # updating needs the current blob sha
        existing = _request(f"{url}?ref={branch}", headers=_gh_headers(token))
        sha = existing.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    payload = {
        "message": "update x_state.json",
        "content": base64.b64encode(
            json.dumps(state).encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    _request(url, method="PUT", headers=_gh_headers(token), payload=payload)
    return True


# --------------------------------------------------------------------------- #
# Public API — always best-effort: the app must still work if the store is down
# --------------------------------------------------------------------------- #
def load_into_file() -> bool:
    """Pull the stored session onto local disk. True if something was written."""
    if not enabled():
        return False
    try:
        state = _gh_load()
    except Exception as e:
        print(f"[x-store] could not read from {describe()}: {e}", flush=True)
        return False
    if not state:
        # Normal the very first time; also seen for a few seconds right after a
        # write, because GitHub's contents API is only eventually consistent.
        print(f"[x-store] nothing stored in {describe()} yet", flush=True)
        return False
    if not state.get("cookies"):
        print(f"[x-store] the session in {describe()} has no cookies — ignoring it",
              flush=True)
        return False
    try:
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.X_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(config.X_STATE_FILE)
    except OSError as e:
        print(f"[x-store] could not write the session locally: {e}", flush=True)
        return False
    print(f"[x-store] restored the X session from {describe()}", flush=True)
    return True


def save_from_file() -> bool:
    """Push the local session to the store. True on success."""
    if not enabled() or not config.X_STATE_FILE.exists():
        return False
    try:
        state = json.loads(config.X_STATE_FILE.read_text())
    except (ValueError, OSError):
        return False
    try:
        ok = _gh_save(state)
    except Exception as e:
        print(f"[x-store] could not save to {describe()}: {e}", flush=True)
        return False
    if ok:
        print(f"[x-store] saved the X session to {describe()}", flush=True)
    return ok


def status() -> dict:
    return {"backend": backend(), "enabled": enabled(), "describe": describe()}
