"""Self-updater: keeps the bot's files in sync with its GitHub repository.

Uses only the Python standard library, so it keeps working even when a package
is broken. bot.py calls startup() before anything else is imported.

HOW IT WORKS - one step per start of the bot ("3 starts"):

    Start 1  CHECK     Compare the local files with the repo. If something is
                       new, changed or deleted -> remember the update.
    Start 2  DOWNLOAD  Download the new/changed files of the NEWEST commit into
                       .update/staged/ and verify every file's checksum.
    Start 3  INSTALL   Back up the old files to .update/backup/, copy the new
                       ones in, delete files that were removed from the repo,
                       run "pip install" if requirements.txt changed - then the
                       bot restarts itself once to load the new version.

While the bot runs, it also checks once a day (cogs/bot_updates.py). A found
update is downloaded in the background ("prepared"); it is installed on the
next start.

SAFETY
- .env, data/, .update/ and caches are never touched.
- Only files that came from GitHub are ever deleted - never your own files.
- Every download is verified against GitHub's checksum.
- Python files are compile-checked before installing; an update with a syntax
  error is refused.
- After installing, the update is "on probation" until the bot has connected
  to Discord with all extensions loaded and run for HEALTHY_AFTER_SECONDS.
  If it crashes, fails to load an extension or doesn't get there within
  MAX_START_ATTEMPTS starts, the old files (and packages) are restored
  automatically and that commit is never tried again. A newer commit is.
- If installing is interrupted (power loss, crash), the next start restores
  the backup.
- Any error in the updater itself is logged and the bot starts normally.

SETTINGS (.env)
    UPDATE_ENABLED   true/false (default: true, but false in a git checkout)
    UPDATE_REPO      owner/name   (default ErikEdits/discord-bot)
    UPDATE_BRANCH    branch       (default claude/sweet-davinci-8vo688)
    UPDATE_GITHUB_TOKEN  optional, only needed for a private repository
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
UPDATE_DIR = ROOT / ".update"
STATE_FILE = UPDATE_DIR / "state.json"
STAGED_DIR = UPDATE_DIR / "staged"
BACKUP_DIR = UPDATE_DIR / "backup"

DEFAULT_REPO = "ErikEdits/discord-bot"
DEFAULT_BRANCH = "claude/sweet-davinci-8vo688"
HEALTHY_AFTER_SECONDS = 60
MAX_START_ATTEMPTS = 2          # starts allowed to become healthy before rolling back
DAILY_CHECK_SECONDS = 24 * 3600
HTTP_TIMEOUT = 20
WINDOWS_RESTART_EXIT_CODE = 3   # start-bot.bat starts the bot again after it exits

# Never downloaded, overwritten or deleted.
EXCLUDED_DIRS = {"data", ".update", ".git", "__pycache__", "venv", ".venv", ".local", "node_modules"}
EXCLUDED_FILES = {".env"}
# Errors that aren't the update's fault (bad token, missing intents) - no rollback.
NOT_UPDATE_ERRORS = {"LoginFailure", "PrivilegedIntentsRequired", "KeyboardInterrupt"}

log = logging.getLogger("setup-bot.updater")
_LOCK = threading.RLock()
_RESTART_REQUESTED = False
_prev_excepthook = sys.excepthook


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _dotenv() -> dict:
    """Minimal .env reader (the updater runs before python-dotenv is loaded)."""
    values = {}
    path = ROOT / ".env"
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(key: str, default: str = "") -> str:
    return os.environ.get(key) or _dotenv().get(key) or default


def repo() -> str:
    return _setting("UPDATE_REPO", DEFAULT_REPO)


def branch() -> str:
    return _setting("UPDATE_BRANCH", DEFAULT_BRANCH)


def enabled() -> bool:
    value = _setting("UPDATE_ENABLED", "").lower()
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    # A git checkout is a developer copy: updating it would fight with git.
    return not (ROOT / ".git").exists()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _default_state() -> dict:
    return {
        "phase": "idle",            # idle | detected | staged | applying | applied
        "installed_commit": None,
        "target": None,             # update found (phase detected)
        "staged": None,             # update downloaded (phase staged)
        "applied": None,            # update installed, waiting to be healthy
        "manifest": {},             # files that came from GitHub: path -> checksum
        "bad_commits": [],
        "last_check": 0,
        "last_error": None,
        "notices": [],              # messages for the admins (sent as DMs by the bot)
        "history": [],
    }


def load_state() -> dict:
    with _LOCK:
        state = _default_state()
        try:
            state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Update state file is unreadable - starting fresh")
        return state


def save_state(state: dict) -> None:
    with _LOCK:
        UPDATE_DIR.mkdir(exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


def _notice(state: dict, level: str, text: str) -> None:
    state["notices"] = (state.get("notices") or [])[-19:] + [{"ts": time.time(), "level": level, "text": text}]
    getattr(log, "error" if level == "error" else "info")("Update: %s", text)


def _history(state: dict, event: str, commit: str | None) -> None:
    state["history"] = (state.get("history") or [])[-29:] + [{"ts": time.time(), "event": event, "commit": commit}]


def pop_notices() -> list[dict]:
    with _LOCK:
        state = load_state()
        notices = state.get("notices") or []
        if notices:
            state["notices"] = []
            save_state(state)
        return notices


def short(commit: str | None) -> str:
    return (commit or "unknown")[:7]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def is_excluded(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(p in EXCLUDED_DIRS for p in parts[:-1]) or parts[0] in EXCLUDED_DIRS:
        return True
    name = parts[-1]
    if name in EXCLUDED_FILES or name.endswith((".pyc", ".pyo", ".updtmp")):
        return True
    return name.startswith(".env") and name != ".env.example"


def blob_sha(data: bytes) -> str:
    """Git's checksum of a file's content (what GitHub reports for each file)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def local_sha(path: str) -> str | None:
    file = ROOT / path
    if not file.is_file():
        return None
    return blob_sha(file.read_bytes())


def _safe_path(path: str) -> Path:
    target = (ROOT / path).resolve()
    if ROOT not in target.parents:
        raise ValueError(f"unsafe path from repository: {path}")
    return target


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".updtmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def compute_changes(tree: dict, manifest: dict) -> dict:
    changed, added = [], []
    for path, sha in tree.items():
        current = local_sha(path)
        if current is None:
            added.append(path)
        elif current != sha:
            changed.append(path)
    # Only delete what came from GitHub earlier and is gone from the repo now.
    deleted = [p for p in manifest if p not in tree and not is_excluded(p) and (ROOT / p).is_file()]
    return {"changed": sorted(changed), "added": sorted(added), "deleted": sorted(deleted)}


def _count(changes: dict) -> str:
    return f"{len(changes['changed'])} changed, {len(changes['added'])} new, {len(changes['deleted'])} deleted"


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _request(url: str, accept: str = "application/vnd.github+json") -> bytes:
    headers = {"User-Agent": "discord-bot-updater", "Accept": accept}
    # Own variable name on purpose: a GITHUB_TOKEN that happens to be set for something
    # else must never break the updates.
    token = _setting("UPDATE_GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404) and "Authorization" in headers:
                # A wrong or expired token - try once more without it (works for public repos).
                log.warning("GitHub refused the token (HTTP %s) - retrying without it", e.code)
                headers.pop("Authorization")
                continue
            if e.code in (401, 403, 404):
                raise RuntimeError(f"GitHub answered HTTP {e.code} for {url}") from e
            last_error = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GitHub not reachable: {last_error}")


def remote_head() -> dict:
    data = json.loads(_request(f"https://api.github.com/repos/{repo()}/commits/{quote(branch(), safe='')}"))
    commit = data["commit"]
    return {
        "commit": data["sha"],
        "tree": commit["tree"]["sha"],
        "message": (commit.get("message") or "").splitlines()[0][:200] if commit.get("message") else "",
        "date": (commit.get("committer") or {}).get("date"),
    }


def remote_tree(tree_sha: str) -> dict:
    data = json.loads(_request(f"https://api.github.com/repos/{repo()}/git/trees/{tree_sha}?recursive=1"))
    if data.get("truncated"):
        raise RuntimeError("repository file list is truncated")
    return {
        e["path"]: e["sha"]
        for e in data.get("tree", [])
        if e.get("type") == "blob" and e.get("mode") in ("100644", "100755") and not is_excluded(e["path"])
    }


def download(commit: str, path: str, expected_sha: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{repo()}/{commit}/{quote(path)}"
    data = _request(url, accept="*/*")
    if blob_sha(data) != expected_sha:
        raise RuntimeError(f"checksum mismatch for {path}")
    return data


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _detect(state: dict) -> bool:
    """Step 1. Returns True if an update was found."""
    head = remote_head()
    state["last_check"] = time.time()
    state["last_error"] = None
    if head["commit"] in state["bad_commits"]:
        log.info("Update %s was rolled back before - waiting for a newer one", short(head["commit"]))
        return False
    tree = remote_tree(head["tree"])
    changes = compute_changes(tree, state["manifest"])
    if not any(changes.values()):
        state.update(phase="idle", target=None, installed_commit=head["commit"], manifest=tree)
        log.info("Bot is up to date (%s)", short(head["commit"]))
        return False
    state["phase"] = "detected"
    state["target"] = {**head, "changes": changes}
    _notice(state, "info", f"Update found: {short(head['commit'])} \"{head['message']}\" ({_count(changes)}). "
                           "It will be downloaded on the next start.")
    _history(state, "detected", head["commit"])
    return True


def _stage(state: dict) -> bool:
    """Step 2: download the newest commit's changed files. Returns True if staged."""
    head = remote_head()
    state["last_check"] = time.time()
    if head["commit"] in state["bad_commits"]:
        state.update(phase="idle", target=None)
        return False
    tree = remote_tree(head["tree"])
    changes = compute_changes(tree, state["manifest"])
    if not any(changes.values()):
        state.update(phase="idle", target=None, staged=None, installed_commit=head["commit"], manifest=tree)
        log.info("Nothing to download - already up to date (%s)", short(head["commit"]))
        return False
    shutil.rmtree(STAGED_DIR, ignore_errors=True)
    files = {}
    for path in changes["changed"] + changes["added"]:
        _safe_path(path)
        data = download(head["commit"], path, tree[path])
        _atomic_write(STAGED_DIR / path, data)
        files[path] = tree[path]
    state["phase"] = "staged"
    state["target"] = None
    state["staged"] = {
        **head,
        "files": files,
        "deleted": changes["deleted"],
        "tree": tree,
        "changes": changes,
        "requirements_changed": "requirements.txt" in files,
        "downloaded_at": time.time(),
    }
    state["last_error"] = None
    _notice(state, "info", f"Update {short(head['commit'])} downloaded ({_count(changes)}). "
                           "It will be installed on the next start.")
    _history(state, "staged", head["commit"])
    return True


def _verify_staged(staged: dict) -> str | None:
    """Return an error text if the downloaded files are incomplete or broken."""
    for path, sha in staged["files"].items():
        file = STAGED_DIR / path
        if not file.is_file() or blob_sha(file.read_bytes()) != sha:
            return f"downloaded file {path} is missing or damaged"
        if path.endswith(".py"):
            try:
                compile(file.read_bytes(), path, "exec")
            except SyntaxError as e:
                return f"{path} has a syntax error (line {e.lineno}): {e.msg}"
    return None


def _pip_install() -> bool:
    req = ROOT / "requirements.txt"
    if not req.is_file():
        return True
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(req)]
    for cmd in (base, base + ["--user"]):
        try:
            result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("pip install failed to run: %s", e)
            continue
        if result.returncode == 0:
            log.info("Installed packages from requirements.txt")
            return True
        log.warning("pip install failed: %s", (result.stderr or result.stdout)[-800:])
    return False


def _apply(state: dict) -> bool:
    """Step 3: install the staged update. Returns True if files changed (-> restart)."""
    staged = state["staged"]
    error = _verify_staged(staged)
    if error:
        if "syntax error" in error:
            state["bad_commits"] = (state["bad_commits"] + [staged["commit"]])[-50:]
            _notice(state, "error", f"Update {short(staged['commit'])} was NOT installed: {error}.")
            _history(state, "refused", staged["commit"])
        else:
            _notice(state, "error", f"Update {short(staged['commit'])} not installed: {error}. It will be downloaded again.")
        state.update(phase="idle", staged=None)
        shutil.rmtree(STAGED_DIR, ignore_errors=True)
        return False

    # Back up everything the update touches, and write down the plan BEFORE changing files.
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    BACKUP_DIR.mkdir(parents=True)
    backup_files, new_files = [], []
    for path in list(staged["files"]) + staged["deleted"]:
        target = _safe_path(path)
        if target.is_file():
            (BACKUP_DIR / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, BACKUP_DIR / path)
            backup_files.append(path)
        elif path in staged["files"]:
            new_files.append(path)
    state["phase"] = "applying"
    state["applied"] = {
        "commit": staged["commit"],
        "message": staged.get("message", ""),
        "previous_commit": state.get("installed_commit"),
        "backup_files": backup_files,
        "new_files": new_files,
        "requirements_changed": staged["requirements_changed"],
        "tree": staged["tree"],
        "changes": staged["changes"],
        "attempts": 0,
        "applied_at": time.time(),
    }
    save_state(state)

    try:
        for path in staged["files"]:
            _atomic_write(_safe_path(path), (STAGED_DIR / path).read_bytes())
        for path in staged["deleted"]:
            target = _safe_path(path)
            if target.is_file():
                target.unlink()
    except Exception as e:
        log.exception("Installing the update failed - restoring the old files")
        _rollback(state, f"installing failed ({e})")
        return True  # files were touched and restored -> restart to be safe

    if staged["requirements_changed"] and not _pip_install():
        _notice(state, "error", "requirements.txt changed but installing the packages failed. "
                                "If the bot doesn't start, the update is rolled back automatically.")
    state["phase"] = "applied"
    state["staged"] = None
    shutil.rmtree(STAGED_DIR, ignore_errors=True)
    _notice(state, "info", f"Update {short(staged['commit'])} installed ({_count(staged['changes'])}). "
                           "Restarting to load it - it's rolled back automatically if it doesn't start.")
    _history(state, "applied", staged["commit"])
    return True


def _rollback(state: dict, reason: str) -> None:
    applied = state.get("applied")
    if applied:
        for path in applied.get("backup_files", []):
            src = BACKUP_DIR / path
            if src.is_file():
                _atomic_write(_safe_path(path), src.read_bytes())
        for path in applied.get("new_files", []):
            try:
                _safe_path(path).unlink()
            except FileNotFoundError:
                pass
        if applied.get("requirements_changed"):
            _pip_install()  # the old requirements.txt is back in place
        state["bad_commits"] = (state["bad_commits"] + [applied["commit"]])[-50:]
        _notice(state, "error", f"Update {short(applied['commit'])} was rolled back: {reason}. "
                                f"The previous version is running again. This update won't be tried again.")
        _history(state, "rolled_back", applied["commit"])
    state.update(phase="idle", applied=None, staged=None, target=None)


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def request_restart() -> None:
    global _RESTART_REQUESTED
    _RESTART_REQUESTED = True


def restart_requested() -> bool:
    return _RESTART_REQUESTED


def restart_process() -> None:
    """Start the bot again in a fresh process with the files currently on disk."""
    log.info("Restarting the bot process")
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        os._exit(WINDOWS_RESTART_EXIT_CODE)  # start-bot.bat starts it again
    argv = list(getattr(sys, "orig_argv", [])) or [sys.executable] + sys.argv
    os.execv(sys.executable, [sys.executable] + argv[1:])


# ---------------------------------------------------------------------------
# Health (called by the bot)
# ---------------------------------------------------------------------------

def pending_health() -> bool:
    return load_state()["phase"] == "applied"


def mark_healthy() -> None:
    with _LOCK:
        state = load_state()
        if state["phase"] != "applied":
            return
        applied = state["applied"]
        state.update(phase="idle", installed_commit=applied["commit"], manifest=applied["tree"], applied=None)
        _notice(state, "info", f"Update {short(applied['commit'])} is running fine ✅")
        _history(state, "healthy", applied["commit"])
        save_state(state)


def report_failure(reason: str) -> bool:
    """Roll back a freshly installed update. Returns True if a rollback happened (-> restart)."""
    with _LOCK:
        state = load_state()
        if state["phase"] not in ("applied", "applying"):
            return False
        _rollback(state, reason)
        save_state(state)
        return True


def _excepthook(exc_type, exc, tb):
    _prev_excepthook(exc_type, exc, tb)
    if exc_type.__name__ in NOT_UPDATE_ERRORS:
        return
    try:
        if report_failure(f"the bot crashed ({exc_type.__name__}: {exc})"):
            restart_process()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def startup() -> None:
    """Run one update step. Called at the very top of bot.py on every start."""
    _setup_logging()
    if not enabled():
        log.info("Auto-update is off (UPDATE_ENABLED=false or git checkout)")
        return
    try:
        with _LOCK:
            state = load_state()
            phase = state["phase"]
            if phase == "applying":
                _rollback(state, "the last installation was interrupted")
                save_state(state)
                restart_process()
            elif phase == "applied":
                state["applied"]["attempts"] += 1
                if state["applied"]["attempts"] > MAX_START_ATTEMPTS:
                    _rollback(state, f"the new version didn't start properly in {MAX_START_ATTEMPTS} tries")
                    save_state(state)
                    restart_process()
                save_state(state)
                sys.excepthook = _excepthook
                log.info("Update %s is on probation (start %d of %d)",
                         short(state["applied"]["commit"]), state["applied"]["attempts"], MAX_START_ATTEMPTS)
            elif phase == "staged":
                restart = _apply(state)
                save_state(state)
                if restart:
                    restart_process()
            elif phase == "detected":
                _stage(state)
                save_state(state)
            else:
                _detect(state)
                save_state(state)
    except Exception as e:
        log.exception("Updater step failed - starting the bot normally")
        try:
            with _LOCK:
                state = load_state()
                if state["phase"] not in ("applying", "applied"):
                    state["last_error"] = f"{type(e).__name__}: {e}"[:300]
                    state["last_check"] = time.time()
                    save_state(state)
        except Exception:
            pass


def background_check() -> str:
    """Daily check while the bot runs: find + download ("prepare"), never install.

    Blocking (network) - run it in a thread. Returns a short status text.
    """
    if not enabled():
        return "Auto-update is off."
    state = load_state()
    phase = state["phase"]
    known_notices = len(state.get("notices") or [])
    if phase in ("applying", "applied"):
        return "An update was just installed and is being checked - try again in a few minutes."
    try:
        if phase == "staged":
            head = remote_head()
            if head["commit"] == state["staged"]["commit"]:
                state["last_check"] = time.time()
                result = f"Update {short(head['commit'])} is ready and will be installed on the next start."
            elif _stage(state):
                result = f"A newer update ({short(state['staged']['commit'])}) was downloaded - installed on the next start."
            else:
                result = "Up to date."
        elif phase == "detected" or _detect(state):
            if _stage(state):
                result = f"Update {short(state['staged']['commit'])} downloaded - it will be installed on the next start."
            else:
                result = "Up to date."
        else:
            result = f"Up to date ({short(state.get('installed_commit'))})."
    except Exception as e:
        log.warning("Update check failed: %s", e)
        state["last_error"] = f"{type(e).__name__}: {e}"[:300]
        state["last_check"] = time.time()
        result = f"Check failed: {e}"
    with _LOCK:
        current = load_state()
        if current["phase"] == phase:  # nothing else changed the state meanwhile
            # Keep what the bot did with the notices in the meantime, add only our new ones.
            state["notices"] = (current.get("notices") or []) + (state.get("notices") or [])[known_notices:]
            save_state(state)
    return result


def status() -> dict:
    state = load_state()
    return {
        "enabled": enabled(),
        "repo": repo(),
        "branch": branch(),
        **{k: state.get(k) for k in ("phase", "installed_commit", "target", "staged", "applied",
                                     "last_check", "last_error", "bad_commits", "history")},
    }
