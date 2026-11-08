#!/usr/bin/env python3
"""Обновления плагина Seller AI: узнать харнесс, обновить плагин его же командой и включить автообновление маркетплейса
там, где у харнесса есть документированная настройка. Печатает одну строку JSON. Только stdlib, Python 3.9+.

    python3 check_update.py [--harness ИМЯ] [--force] [--dry-run]

--harness   claude-code, claude-app, codex, vscode, copilot-cli, cursor, hermes, opencode, mcp-skills. Без него харнесс
            узнаётся по пути этого файла (папка установки плагина), по окружению и по установленным копиям.
--force     проверить, даже если харнесс обновлялся меньше 30 минут назад.
--dry-run   ничего не менять: выполняются только чтения, а что было бы сделано — в поле would.

Ответ: {"harness", "installed", "now", "status", "auto_update", "changed", "next_step", "notes"}.
status: up_to_date | updated | skipped | needs_user | unknown_harness | error;
auto_update: on | enabled_now | unavailable | needs_user;
changed — что изменено на этой машине: файл, ключ, было → стало; версия плагина; ссылки навыков.
Код выхода: 0; 1 — error; 2 — unknown_harness.

Своего состояния скрипт не ведёт: «проверяли недавно» он узнаёт по отметкам самого харнесса (lastUpdated маркетплейса
Claude Code, .git/FETCH_HEAD клона, запись установки Codex), а в сеть ходит только командами харнесса (claude, codex,
copilot, hermes) и git. SELLER_AI_PLUGIN_REPO — с каким репозиторием маркетплейса сверять копии Cursor из маркетплейса
(по умолчанию публичный alvenstudio/seller-ai-plugin).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = "seller-ai"
REPO = "https://github.com/alvenstudio/seller-ai-plugin.git"
REPO_SLUG = "alvenstudio/seller-ai-plugin"
FRESH = 30 * 60  # не чаще раза в 30 минут (владелец, 08.11.2026)
UPDATE_TIMEOUT = 300
READ_TIMEOUT = 60
# Харнесс → файл с его особенностями (от папки навыка).
HARNESSES = {
    "claude-code": "harnesses/claude-code.md",
    "claude-app": "harnesses/claude-app.md",
    "codex": "harnesses/codex.md",
    "vscode": "harnesses/vscode-copilot.md",
    "copilot-cli": "harnesses/vscode-copilot.md",
    "cursor": "harnesses/cursor.md",
    "hermes": "harnesses/hermes.md",
    "opencode": "harnesses/opencode.md",
    "mcp-skills": "harnesses/other.md",
}
ALIASES = {"claude": "claude-code", "claude-ai": "claude-app", "claude.ai": "claude-app", "cowork": "claude-app",
           "chatgpt": "codex", "copilot": "copilot-cli", "vs-code": "vscode", "mcp": "mcp-skills",
           "other": "mcp-skills"}
RELOAD_CLAUDE = ("Эта сессия Claude Code до перезагрузки работает на прежних навыках: попроси пользователя ввести "
                 "/reload-plugins (агент сам не может) или начать новую сессию.")
ZERO = (0, 0, 0)


class Ctx:
    def __init__(self, env, now, dry_run=False, force=False, script=None):
        self.env = dict(env)
        self.home = Path(self.env.get("HOME") or Path.home())
        self.now = now
        self.dry_run = dry_run
        self.force = force
        self.script = Path(script or __file__)


# ---- мелочи ---------------------------------------------------------------------------------------------------------

def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_object(path):
    """JSON-объект настроек: (dict, None); файла нет — ({}, None); не читается — (None, причина)."""
    path = Path(path)
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, str(e)
    return (data, None) if isinstance(data, dict) else (None, "в файле не объект JSON")


def write_json(path, data):
    """Запись целиком через временный файл рядом; права файла сохраняются, ссылка на файл (dotfiles) остаётся ссылкой."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def manifest(root):
    for rel in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json"):
        data = load_json(Path(root) / rel)
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            return data
    return None


def manifest_version(root):
    data = manifest(root)
    return data["version"] if data else None


def vtuple(v):
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b):
    """a новее b; неизвестная версия — не новее."""
    ta, tb = vtuple(a), vtuple(b)
    return bool(ta and tb and ta > tb)


def highest(versions, default=None):
    versions = [v for v in versions if v]
    return max(versions, key=lambda v: vtuple(v) or ZERO) if versions else default


def iso_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def is_under(path, root):
    for a in {str(path), os.path.realpath(path)}:
        for b in {str(root), os.path.realpath(root)}:
            b = b.rstrip(os.sep) or os.sep
            if a == b or a.startswith(b + os.sep):
                return True
    return False


def tilde(ctx, path):
    s, h = str(path), str(ctx.home).rstrip(os.sep)
    return "~" + s[len(h):] if h and (s == h or s.startswith(h + os.sep)) else s


def shown(cmd):
    return " ".join(shlex.quote(str(c)) for c in [Path(cmd[0]).name, *cmd[1:]])


def tail(text, n=300):
    text = " ".join((text or "").split())
    return text if len(text) <= n else "…" + text[-n:]


def which(ctx, name):
    return shutil.which(name, path=ctx.env.get("PATH", os.defpath))


def run(ctx, cmd, timeout=READ_TIMEOUT):
    """(код, stdout, stderr); нет команды или не ответила — код None. stdin закрыт: команда не ждёт ответа человека."""
    env = dict(ctx.env)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        p = subprocess.run([str(c) for c in cmd], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"не ответила за {timeout} с"
    except OSError as e:
        return None, "", str(e)


def note(r, text):
    r["notes"].append(text)


def note_run(r, cmd, code, out, err):
    note(r, f"выполнено: {shown(cmd)} → код {code}" + ("" if code == 0 else ": " + tail(err or out)))


def note_mark(ctx, r, what, ts):
    if ts is None:
        note(r, f"отметки «{what}» нет")
        return
    when = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    note(r, f"отметка — {what}: {when}, {max(0, int((ctx.now - ts) // 60))} мин назад")


def skip(ctx, ts, pending=False):
    """Харнесс обновлялся меньше 30 минут назад, и новее локально ничего не лежит: проверка не нужна."""
    return not ctx.force and ts is not None and 0 <= ctx.now - ts < FRESH and not pending


def would(r, text):
    r["would"].append(text)


def change(ctx, r, what):
    """Изменение настройки: в --dry-run — в would, иначе в changed."""
    (r["would"] if ctx.dry_run else r["changed"]).append(what)


def script_roots(ctx):
    """Корни плагина, из которого запущен скрипт (<корень>/skills/auto-update/scripts/check_update.py): как вызвали и
    после разворота ссылок."""
    roots = []
    for p in (ctx.script, Path(os.path.realpath(ctx.script))):
        if len(p.parents) > 3 and p.parent.name == "scripts" and p.parents[1].name == "auto-update" \
                and p.parents[2].name == "skills" and p.parents[3] not in roots:
            roots.append(p.parents[3])
    return roots


def mine_first(ctx, folders):
    """Папка, в которой лежит этот скрипт (копия этой сессии), иначе None."""
    roots = script_roots(ctx)
    return next((f for f in folders if any(is_under(root, f) for root in roots)), None)


def session_version(ctx, areas):
    """Версия копии, из которой запущен скрипт, если она лежит в папках установки харнесса; иначе (исходники, архив)
    это не копия сессии."""
    for root in script_roots(ctx):
        if any(is_under(root, a) for a in areas):
            return manifest_version(root)
    return None


def unchanged(r, status, session, reload_step):
    """Итог без обновления: на диске новее, чем загрузила сессия, — сессию перезагрузить."""
    if newer(r["installed"], session):
        r["status"] = "needs_user"
        r["next_step"] = f"На диске уже {r['installed']}, а сессия загрузила {session}. " + reload_step
    else:
        r["status"] = status


def planned(r):
    r["status"], r["next_step"] = "needs_user", "Без --dry-run скрипт выполнит: " + "; ".join(r["would"])


def git(ctx, repo, *args, timeout=READ_TIMEOUT):
    return run(ctx, ["git", "-C", str(repo), *args], timeout)


def git_head(ctx, repo):
    code, out, _ = git(ctx, repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


def git_remote_head(ctx, repo=None, url=None):
    """HEAD удалённого репозитория (только чтение, git ls-remote): origin клона repo или репозиторий url."""
    if url:
        code, out, _ = run(ctx, ["git", "ls-remote", str(url), "HEAD"])
    else:
        code, out, _ = git(ctx, repo, "ls-remote", "origin", "HEAD")
    if code != 0:
        return None
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "HEAD" and sha.strip():
            return sha.strip()
    return None


def git_moved(ctx, repo):
    """У origin другой HEAD, чем у клона: True; тот же — False; не узнать (нет git, сети) — None."""
    if not (Path(repo) / ".git").exists():
        return None
    local, remote = git_head(ctx, repo), git_remote_head(ctx, repo)
    if not local or not remote:
        return None
    return local != remote


def git_pull(ctx, r, repo):
    """git pull --ff-only клона; False — не прошёл (r заполнен)."""
    cmd = ["git", "-C", str(repo), "pull", "--ff-only", "--quiet"]
    code, out, err = run(ctx, cmd, UPDATE_TIMEOUT)
    note_run(r, cmd, code, out, err)
    if code == 0:
        return True
    r["status"] = "needs_user"
    r["next_step"] = (f"`git -C {shlex.quote(tilde(ctx, repo))} pull --ff-only` не прошёл: {tail(err or out, 200)}. "
                      "Посмотри `git status` в этой папке, разберись с локальными правками или веткой и повтори.")
    return False


def set_marketplace_auto(ctx, r, settings, settings_path, mp, entry, source, source_text):
    """extraKnownMarketplaces.<mp>.autoUpdate = true (Claude Code, Copilot CLI); пишет, что изменено."""
    new = dict(entry) if isinstance(entry, dict) else {"source": source}
    was = json.dumps(entry["autoUpdate"]) if isinstance(entry, dict) and "autoUpdate" in entry else "нет"
    new["autoUpdate"] = True
    what = f"{tilde(ctx, settings_path)}: extraKnownMarketplaces.{mp}.autoUpdate: {was} → true"
    if not isinstance(entry, dict):
        what += f" (запись маркетплейса добавлена: {source_text})"
    change(ctx, r, what)
    if not ctx.dry_run:
        ekm = dict(settings.get("extraKnownMarketplaces") or {})
        ekm[mp] = new
        settings["extraKnownMarketplaces"] = ekm
        write_json(settings_path, settings)


# ---- Claude Code ----------------------------------------------------------------------------------------------------

def claude_dirs(ctx):
    cdir = Path(ctx.env.get("CLAUDE_CONFIG_DIR") or ctx.home / ".claude")
    return cdir, Path(ctx.env.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or cdir / "plugins")


def claude_cli(ctx):
    """Команда claude той же сборки, что ведёт сессию (приложение Claude даёт свою в CLAUDE_CODE_EXECPATH), иначе из
    PATH."""
    exe = ctx.env.get("CLAUDE_CODE_EXECPATH")
    if exe and Path(exe).name.startswith("claude") and os.access(exe, os.X_OK):
        return exe
    return which(ctx, "claude")


def claude_entries(ctx, cli, proot, cdir):
    """Установки seller-ai: из `claude plugin list --json`, без команды — из записей Claude Code на диске."""
    if cli:
        code, out, _ = run(ctx, [cli, "plugin", "list", "--json"])
        if code == 0 and "[" in out:
            try:
                rows = json.loads(out[out.index("["):])
            except ValueError:
                rows = None
            if isinstance(rows, list):
                return [e for e in rows if isinstance(e, dict) and str(e.get("id", "")).startswith(PLUGIN + "@")]
    entries = []
    settings, _ = read_object(cdir / "settings.json")
    enabled = (settings or {}).get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    data = load_json(proot / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    for pid, rows in (plugins if isinstance(plugins, dict) else {}).items():
        if pid.startswith(PLUGIN + "@") and isinstance(rows, list):
            entries += [dict(row, id=pid, enabled=enabled.get(pid, True)) for row in rows if isinstance(row, dict)]
    for found in sorted(proot.glob(f"synced/*/{PLUGIN}/.claude-plugin/plugin.json")):
        entries.append({"id": PLUGIN + "@synced", "version": (load_json(found) or {}).get("version"),
                        "scope": "synced", "installPath": str(found.parents[1]), "enabled": True})
    for found in sorted((cdir / "skills").glob("*/.claude-plugin/plugin.json")):
        data = load_json(found) or {}
        if data.get("name") == PLUGIN:
            entries.append({"id": PLUGIN + "@skills-dir", "version": data.get("version"), "scope": "user",
                            "installPath": str(found.parents[1]), "enabled": True})
    return entries


def origin_of(entry):
    return str(entry.get("id", "")).split("@", 1)[-1]


def pick_entry(ctx, entries):
    """Установка этой сессии: та, в папке которой лежит скрипт; иначе включённая из маркетплейса (она главнее копии из
    claude.ai), потом синхронизированная, потом любая."""
    mine = mine_first(ctx, [e["installPath"] for e in entries if e.get("installPath")])
    if mine:
        return next(e for e in entries if e.get("installPath") == mine)
    market = [e for e in entries if origin_of(e) not in ("synced", "inline", "skills-dir")]
    synced = [e for e in entries if origin_of(e) == "synced"]
    for group in (market, synced, entries):
        live = [e for e in group if e.get("enabled") is not False]
        if live:
            return max(live, key=lambda e: vtuple(e.get("version")) or ZERO)
    return entries[0] if entries else None


def marketplace_version(mp_root):
    """Версия плагина в локальной копии маркетплейса (её Claude Code обновляет при проверке маркетплейса)."""
    mp_root = Path(mp_root)
    market = load_json(mp_root / ".claude-plugin/marketplace.json")
    for p in (market.get("plugins") or []) if isinstance(market, dict) else []:
        if isinstance(p, dict) and p.get("name") == PLUGIN:
            found = manifest_version(mp_root / p["source"]) if isinstance(p.get("source"), str) else None
            if found or isinstance(p.get("version"), str):
                return found or p["version"]
    return manifest_version(mp_root)


def claude_auto(ctx, r, settings_path, mp, rec):
    """autoUpdate маркетплейса: запись в extraKnownMarketplaces настроек главнее known_marketplaces.json; у сторонних
    маркетплейсов по умолчанию выключено. Переменные, выключающие проход автообновления, решает пользователь."""
    settings, err = read_object(settings_path)
    ekm = settings.get("extraKnownMarketplaces") if settings is not None else None
    entry = ekm.get(mp) if isinstance(ekm, dict) else None
    s_flag = entry.get("autoUpdate") if isinstance(entry, dict) else None
    manual = f"Включить автообновление маркетплейса: /plugin → Marketplaces → {mp} → Enable auto-update."
    if s_flag is True or (s_flag is None and rec.get("autoUpdate") is True):
        r["auto_update"] = "on"
    elif settings is None or (ekm is not None and not isinstance(ekm, dict)):
        r["auto_update"], r["_auto"] = "needs_user", manual
        note(r, f"{tilde(ctx, settings_path)} не правлю: {err or 'extraKnownMarketplaces — не объект'}")
    elif isinstance(entry, dict) or isinstance(rec.get("source"), dict):
        set_marketplace_auto(ctx, r, settings, settings_path, mp, entry, rec.get("source"),
                             "source из known_marketplaces.json")
        r["auto_update"] = "needs_user" if ctx.dry_run else "enabled_now"
        if not ctx.dry_run:
            note(r, "автообновление сработает в следующих сессиях Claude Code: проход — раз за сессию, через случайные "
                    "≤10 минут после первого сообщения")
    else:
        r["auto_update"], r["_auto"] = "needs_user", manual
        note(r, f"у маркетплейса {mp} нет source в known_marketplaces.json")
    off = [k for k in ("DISABLE_UPDATES", "DISABLE_AUTOUPDATER") if truthy(ctx.env.get(k))]
    if ctx.env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):  # любое непустое значение, и «0» тоже
        off.append("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")
    if off == ["DISABLE_AUTOUPDATER"] and ctx.env.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop":
        # Приложение Claude запускает каждую сессию с DISABLE_AUTOUPDATER=1 — Claude Code обновляет оно само, а проход
        # автообновления плагинов работает (08.11.2026: оба маркетплейса с autoUpdate обновлены через 25 с после
        # старта сессии, seller-ai 1.15.2 → 1.16.0).
        note(r, "DISABLE_AUTOUPDATER=1 ставит само приложение Claude (оно обновляет Claude Code); плагины обновляет "
                "проход автообновления сессии")
        off = []
    if off and not truthy(ctx.env.get("FORCE_AUTOUPDATE_PLUGINS")):
        r["auto_update"] = "needs_user"
        note(r, f"{', '.join(off)} выключает проход автообновления Claude Code целиком — переменные окружения скрипт не "
                "меняет")
        r["_auto"] = " ".join(filter(None, [r.get("_auto"), (
            f"Автообновление Claude Code выключено ({', '.join(off)}): плагины обновляются сами, если убрать переменную "
            "или добавить FORCE_AUTOUPDATE_PLUGINS=1; иначе их обновляет этот навык раз в 30 минут работы.")]))


def claude_code(ctx, r):
    cdir, proot = claude_dirs(ctx)
    cli = claude_cli(ctx)
    entries = claude_entries(ctx, cli, proot, cdir)
    entry = pick_entry(ctx, entries)
    session = session_version(ctx, [proot, cdir / "skills"])
    if session:
        note(r, f"эта сессия загрузила {session}")
    install = f"`claude plugin install {PLUGIN} --marketplace {REPO_SLUG}`"
    if entry is None:
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=f"В Claude Code плагин Seller AI не установлен из маркетплейса и сам не обновится. "
                           f"Установить: {install}, затем /plugin → Marketplaces → {PLUGIN} → Enable auto-update "
                           "и /reload-plugins.")
        return
    origin = origin_of(entry)
    r["installed"] = entry.get("version")
    note(r, f"установка {entry.get('id')} ({entry.get('scope') or 'scope ?'}): "
            f"{tilde(ctx, entry.get('installPath') or '?')}")
    if origin == "synced":
        claude_synced(ctx, r, entry, session)
        return
    if origin in ("inline", "skills-dir"):
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=f"Эта копия ({entry.get('id')}) загружена не из маркетплейса и сама не обновится. Поставь "
                           f"плагин из маркетплейса: {install}, включи /plugin → Marketplaces → {PLUGIN} → Enable "
                           f"auto-update, убери старую копию ({tilde(ctx, entry.get('installPath') or '?')}) и введи "
                           "/reload-plugins.")
        return
    mp = origin
    known = load_json(proot / "known_marketplaces.json")
    rec = known.get(mp) if isinstance(known, dict) and isinstance(known.get(mp), dict) else {}
    claude_auto(ctx, r, cdir / "settings.json", mp, rec)
    mp_root = Path(rec.get("installLocation") or proot / "marketplaces" / mp)
    mp_version = marketplace_version(mp_root)
    pending = newer(mp_version, r["installed"])
    if pending:
        note(r, f"в копии маркетплейса уже {mp_version}")
    mark = iso_ts(rec.get("lastUpdated"))
    note_mark(ctx, r, f"lastUpdated маркетплейса {mp} в known_marketplaces.json", mark)
    if skip(ctx, mark, pending):
        unchanged(r, "skipped", session, RELOAD_CLAUDE)
        return
    scope = ["--scope", entry["scope"]] if entry.get("scope") in ("user", "project", "local") else []
    cmds = [[cli or "claude", "plugin", "marketplace", "update", mp, "--json"],
            [cli or "claude", "plugin", "update", f"{PLUGIN}@{mp}", *scope, "--json"]]
    if ctx.dry_run:
        moved = True if pending else git_moved(ctx, mp_root)
        if moved is False:
            note(r, "копия маркетплейса совпадает с его репозиторием (git ls-remote)")
            unchanged(r, "up_to_date", session, RELOAD_CLAUDE)
            return
        if moved is None:
            note(r, "есть ли версия новее, без обновления маркетплейса не узнать (git ls-remote не ответил)")
        for cmd in cmds:
            would(r, shown(cmd))
        planned(r)
        return
    if cli is None:
        r["status"] = "needs_user"
        r["next_step"] = (f"Команды claude нет в PATH. В сессии: /plugin → Installed → {PLUGIN} → Update now (или "
                          f"Marketplaces → {mp} → Update marketplace), затем /reload-plugins.")
        return
    failed = None
    for cmd in cmds:
        code, out, err = run(ctx, cmd, UPDATE_TIMEOUT)
        note_run(r, cmd, code, out, err)
        if code != 0:
            failed = tail(err or out, 200)
    after = [e for e in claude_entries(ctx, cli, proot, cdir) if e.get("id") == entry.get("id")]
    same = [e for e in after if e.get("scope") == entry.get("scope")] or after
    r["now"] = highest((e.get("version") for e in same), r["installed"])
    if newer(r["now"], r["installed"]):
        r["status"] = "updated"
        r["changed"].append(f"плагин {PLUGIN}@{mp}: {r['installed']} → {r['now']} (claude plugin update)")
        r["next_step"] = f"Новая версия {r['now']} на диске. " + RELOAD_CLAUDE
    elif failed:
        r["status"] = "error"
        r["next_step"] = (f"Обновление не прошло ({failed}). В сессии: /plugin → Installed → {PLUGIN} → Update now; "
                          "причину покажет `claude plugin update` в терминале.")
    else:
        unchanged(r, "up_to_date", session, RELOAD_CLAUDE)


def claude_synced(ctx, r, entry, session):
    """seller-ai@synced — плагин из аккаунта claude.ai: обновляет его claude.ai, Claude Code синхронизирует при запуске."""
    r["auto_update"] = "needs_user"
    data = load_json(Path(entry.get("installPath") or ".").parent / "manifest.json")
    stamp = data.get("lastUpdated") if isinstance(data, dict) else None
    ts = stamp / 1000 if isinstance(stamp, (int, float)) else None
    note_mark(ctx, r, "синхронизация с claude.ai (manifest.json)", ts)
    note(r, "«Sync automatically» маркетплейса видно и меняет только claude.ai")
    if skip(ctx, ts):
        unchanged(r, "skipped", session, RELOAD_CLAUDE)
        return
    r["status"] = "needs_user"
    r["next_step"] = ("Плагин приходит из аккаунта claude.ai (seller-ai@synced), обновляет его claude.ai: Настроить → "
                      "Плагины → маркетплейс seller-ai → «Check for updates»; там же включи «Sync automatically», "
                      "чтобы новые версии приходили сами. Claude Code забирает их при каждом запуске (в открытой "
                      "сессии — /reload-plugins), Cowork — со следующей задачи.")


def claude_app(ctx, r):
    """claude.ai, чат Claude Desktop, Cowork: терминала обычно нет, обновляет только пользователь в claude.ai."""
    r.update(status="needs_user", auto_update="needs_user",
             next_step="Настроить → Плагины → маркетплейс alvenstudio/seller-ai-plugin → «Check for updates»; там же "
                       "«Sync automatically», чтобы новые версии приходили сами. Новую версию берут новые разговоры, "
                       "Cowork — следующая задача. Плагин, загруженный архивом (Upload plugin), не обновляется: "
                       "добавь маркетплейс alvenstudio/seller-ai-plugin и поставь плагин из него.")


# ---- Codex ----------------------------------------------------------------------------------------------------------

def codex_marketplaces(path):
    """[marketplaces.*] из config.toml: tomllib (3.11+) или построчно."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import tomllib
        found = tomllib.loads(text).get("marketplaces")
        return found if isinstance(found, dict) else {}
    except ImportError:
        pass
    except ValueError:
        return {}
    out, cur = {}, None
    for line in text.splitlines():
        head = re.match(r'^\s*\[marketplaces\.(?:"([^"]+)"|([A-Za-z0-9_.-]+))\]\s*(#.*)?$', line)
        if head:
            cur = head.group(1) or head.group(2)
            out[cur] = {}
        elif re.match(r"^\s*\[", line):
            cur = None
        elif cur:
            kv = re.match(r'^\s*([A-Za-z_]+)\s*=\s*"([^"]*)"', line)
            if kv:
                out[cur][kv.group(1)] = kv.group(2)
    return out


def codex_store(ctx):
    """Папка установок Codex: ~/.codex/plugins/cache/<маркетплейс>/<плагин>/<версия>/ (CODEX_HOME вместо ~/.codex)."""
    return Path(ctx.env.get("CODEX_HOME") or ctx.home / ".codex") / "plugins/cache"


def codex_installs(store):
    """(маркетплейс, папка версии, версия) копий seller-ai."""
    found = []
    for man in sorted(Path(store).glob(f"*/{PLUGIN}/*/.codex-plugin/plugin.json")):
        version = (load_json(man) or {}).get("version")
        if isinstance(version, str):
            found.append((man.parents[3].name, man.parents[1], version))
    return found


def codex(ctx, r):
    ch = Path(ctx.env.get("CODEX_HOME") or ctx.home / ".codex")
    store = codex_store(ctx)
    found = codex_installs(store)
    if not found:
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=f"В Codex плагин Seller AI не установлен из маркетплейса: `codex plugin marketplace add "
                           f"{REPO_SLUG}`, `codex plugin add {PLUGIN}@{PLUGIN}`, `codex mcp login {PLUGIN}`; затем "
                           "новая сессия.")
        return
    folder = mine_first(ctx, [f[1] for f in found])
    mp = next(f[0] for f in found if f[1] == folder) if folder else \
        max(found, key=lambda f: vtuple(f[2]) or ZERO)[0]
    session = session_version(ctx, [store])
    if session:
        note(r, f"эта сессия загрузила {session}")
    r["installed"] = highest(f[2] for f in found if f[0] == mp)
    note(r, f"установка {PLUGIN}@{mp}: {tilde(ctx, store / mp / PLUGIN)}")
    snapshot = ch / ".tmp/marketplaces" / mp
    if (codex_marketplaces(ch / "config.toml").get(mp) or {}).get("source_type") == "local":
        r["auto_update"] = "unavailable"
        note(r, f"маркетплейс {mp} добавлен из локальной папки — Codex его не обновляет")
        r["_auto"] = (f"Чтобы Codex обновлял плагин сам, добавь маркетплейс из GitHub: `codex plugin marketplace add "
                      f"{REPO_SLUG}`.")
    else:
        r["auto_update"] = "on"
        note(r, "Codex обновляет Git-маркетплейсы при каждом запуске CLI и app-server; приложение ChatGPT, открытое "
                "давно, остаётся на версии своего запуска")
    pending = newer(manifest_version(snapshot / "codex") or manifest_version(snapshot), r["installed"])
    mark = mtime(snapshot / ".codex-marketplace-install.json")
    note_mark(ctx, r, "последняя смена ревизии маркетплейса (.codex-marketplace-install.json)", mark)
    reload_step = "Новую версию берёт новая сессия Codex или новый чат; приложение ChatGPT — перезапустить."
    if skip(ctx, mark, pending):
        unchanged(r, "skipped", session, reload_step)
        return
    cmd = ["codex", "plugin", "marketplace", "upgrade", mp]
    manual = (f"Выполни `{shown(cmd)}` с одобрением пользователя — вне песочницы: нужны сеть и запись в "
              f"{tilde(ctx, ch)}; или пусть пользователь выполнит её в терминале (в TUI: /plugins → маркетплейс {mp} → "
              f"Ctrl+U). {reload_step}")
    if ctx.dry_run:
        would(r, shown(cmd + ["--json"]))
        r["status"], r["next_step"] = "needs_user", manual
        return
    cli = which(ctx, "codex")
    sandboxed = bool(ctx.env.get("CODEX_SANDBOX") or ctx.env.get("CODEX_SANDBOX_NETWORK_DISABLED"))
    if sandboxed or cli is None:
        note(r, "песочница Codex: сети нет, запись вне рабочей папки — с одобрением" if sandboxed
             else "команды codex нет в PATH")
        r["status"], r["next_step"] = "needs_user", manual
        return
    full = [cli, *cmd[1:], "--json"]
    code, out, err = run(ctx, full, UPDATE_TIMEOUT)
    note_run(r, full, code, out, err)
    r["now"] = highest((f[2] for f in codex_installs(store) if f[0] == mp), None)
    if newer(r["now"], r["installed"]):
        r["status"] = "updated"
        r["changed"].append(f"плагин {PLUGIN}@{mp}: {r['installed']} → {r['now']} (codex plugin marketplace upgrade)")
        r["next_step"] = f"Новая версия {r['now']} на диске. {reload_step}"
    elif code != 0:
        r["status"], r["next_step"] = "error", manual
    else:
        unchanged(r, "up_to_date", session, reload_step)


# ---- VS Code и Copilot CLI ------------------------------------------------------------------------------------------

def vscode_auto(ctx, r):
    """extensions.autoUpdate в пользовательских настройках VS Code: ключ общий для всех расширений — не меняется."""
    bases = [ctx.home / "Library/Application Support", Path(ctx.env.get("XDG_CONFIG_HOME") or ctx.home / ".config")]
    if ctx.env.get("APPDATA"):
        bases.append(Path(ctx.env["APPDATA"]))
    for base in bases:
        for product in ("Code", "Code - Insiders"):
            path = base / product / "User/settings.json"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
            m = re.search(r'"extensions\.autoUpdate"\s*:\s*(true|false|"[^"]*")', text)
            if m and m.group(1) in ("false", '"off"'):
                r["auto_update"] = "needs_user"
                note(r, f"в {tilde(ctx, path)} extensions.autoUpdate: {m.group(1)} — ключ общий для всех расширений, "
                        "скрипт его не меняет")
                r["_auto"] = ("Автообновление VS Code выключено (extensions.autoUpdate): включить ли его для всех "
                              "расширений, решает пользователь; пока — «Chat: Update Plugins» вручную.")
                return
    r["auto_update"] = "on"
    note(r, "VS Code обновляет плагины примерно раз в 24 часа (extensions.autoUpdate не выключен)")


def vscode(ctx, r):
    clones = []
    for base in (ctx.home / ".vscode/agent-plugins", ctx.home / ".vscode-insiders/agent-plugins"):
        clones += [m.parent for m in sorted(base.glob("*/*/*/plugin.json")) if (load_json(m) or {}).get("name") == PLUGIN]
    if not clones:
        inventory = Path(ctx.env.get("COPILOT_HOME") or ctx.home / ".copilot") / "installed-plugins"
        if any(inventory.glob(f"*/{PLUGIN}")):
            note(r, "плагин VS Code — из установок Copilot CLI (~/.copilot/installed-plugins)")
            copilot_cli(ctx, r)
            return
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=f"В VS Code плагин Seller AI не найден: в чате Copilot `/plugin marketplace add {REPO_SLUG}`, "
                           f"затем `/plugin install {PLUGIN}@{PLUGIN}`.")
        return
    clone = mine_first(ctx, clones) or clones[0]
    r["installed"] = manifest_version(clone)
    note(r, f"клон маркетплейса VS Code: {tilde(ctx, clone)}")
    vscode_auto(ctx, r)
    mark = mtime(clone / ".git/FETCH_HEAD")
    note_mark(ctx, r, "последнее обновление клона (.git/FETCH_HEAD)", mark)
    if skip(ctx, mark):
        r["status"] = "skipped"
        return
    step = ("В VS Code: палитра команд → «Chat: Update Plugins» (или Agent Customizations → Plugins → «Check for and "
            "Apply Updates»). Изменённые файлы плагина VS Code подхватывает сам; если навык прежний — новая сессия чата "
            "или «Developer: Reload Window».")
    moved = git_moved(ctx, clone)
    if moved is False:
        note(r, "клон совпадает с репозиторием маркетплейса (git ls-remote)")
        r["status"] = "up_to_date"
    elif moved:
        note(r, "в репозитории маркетплейса новее, чем в клоне (git ls-remote); папку VS Code скрипт не трогает")
        r["status"], r["next_step"] = "needs_user", step
    else:
        note(r, "не сверить с репозиторием маркетплейса: git ls-remote не ответил")
        r["status"], r["next_step"] = "error", step


def copilot_auto(ctx, r, settings_path, mp):
    """Copilot CLI обновляет плагины сторонних маркетплейсов только по согласию: extraKnownMarketplaces.<имя>.autoUpdate
    в ~/.copilot/settings.json; проверка — при старте интерактивной сессии (или -p) в доверенной папке."""
    settings, err = read_object(settings_path)
    ekm = settings.get("extraKnownMarketplaces") if settings is not None else None
    entry = ekm.get(mp) if isinstance(ekm, dict) else None
    manual = f"Включить автообновление: в {tilde(ctx, settings_path)} — extraKnownMarketplaces.{mp}.autoUpdate: true."
    if settings is None or (ekm is not None and not isinstance(ekm, dict)):
        r["auto_update"], r["_auto"] = "needs_user", manual
        note(r, f"{tilde(ctx, settings_path)} не правлю: {err or 'extraKnownMarketplaces — не объект'}")
    elif isinstance(entry, dict) and entry.get("autoUpdate") is True:
        r["auto_update"] = "on"
    elif isinstance(entry, dict) or mp == PLUGIN:
        set_marketplace_auto(ctx, r, settings, settings_path, mp, entry, {"source": "github", "repo": REPO_SLUG},
                             f"github {REPO_SLUG}")
        r["auto_update"] = "needs_user" if ctx.dry_run else "enabled_now"
        if not ctx.dry_run:
            note(r, "сработает при старте следующей интерактивной сессии (или -p) в доверенной папке; сессии SDK и "
                    "сервера и запуски в CI не обновляются")
    else:
        r["auto_update"], r["_auto"] = "needs_user", manual
        note(r, f"источник маркетплейса {mp} неизвестен")
    env_off = str(ctx.env.get("COPILOT_AUTO_UPDATE", "")).strip().lower() in ("false", "0", "no", "off")
    if env_off or (settings or {}).get("autoUpdate") is False:
        why = "COPILOT_AUTO_UPDATE=false" if env_off else f"autoUpdate: false в {tilde(ctx, settings_path)}"
        r["auto_update"] = "needs_user"
        note(r, f"{why} — общий выключатель обновлений Copilot CLI, скрипт его не меняет")
        r["_auto"] = " ".join(filter(None, [r.get("_auto"), (
            f"Автообновление Copilot CLI выключено ({why}): включить ли, решает пользователь; пока плагин обновляет "
            "этот навык раз в 30 минут работы.")]))


def copilot_cli(ctx, r):
    ch = Path(ctx.env.get("COPILOT_HOME") or ctx.home / ".copilot")
    inventory = ch / "installed-plugins"
    dirs = [d for d in sorted(inventory.glob(f"*/{PLUGIN}")) if d.parent.name != "_direct" and manifest_version(d)]
    if not dirs:
        direct = [d for d in sorted(inventory.glob("_direct/*")) if (manifest(d) or {}).get("name") == PLUGIN]
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=(f"Плагин Seller AI поставлен в Copilot CLI не из маркетплейса ({tilde(ctx, direct[0])}) и сам "
                            "не обновится" if direct else "В Copilot CLI плагин Seller AI не установлен") +
                           f": `copilot plugin marketplace add {REPO_SLUG}`, затем `copilot plugin install "
                           f"{PLUGIN}@{PLUGIN}`.")
        return
    folder = mine_first(ctx, dirs) or dirs[0]
    mp = folder.parent.name
    r["installed"] = manifest_version(folder)
    note(r, f"установка Copilot CLI: {tilde(ctx, folder)}")
    copilot_auto(ctx, r, ch / "settings.json", mp)
    stamps = [mtime(folder / rel) for rel in ("plugin.json", ".claude-plugin/plugin.json")]
    mark = max((s for s in stamps if s), default=None)
    note_mark(ctx, r, "последняя установка плагина (его plugin.json)", mark)
    reload_step = "Новую версию берёт новая сессия Copilot CLI или /restart; VS Code подхватывает файлы сам."
    if skip(ctx, mark):
        r["status"] = "skipped"
        return
    cmds = [["copilot", "plugin", "marketplace", "update", mp], ["copilot", "plugin", "update", PLUGIN]]
    if ctx.dry_run:
        for cmd in cmds:
            would(r, shown(cmd))
        planned(r)
        return
    cli = which(ctx, "copilot")
    if cli is None:
        r["status"] = "needs_user"
        r["next_step"] = (f"Команды copilot нет в PATH: обнови в Copilot CLI — `/plugin update {PLUGIN}`, или в VS Code — "
                          "«Chat: Update Plugins». " + reload_step)
        return
    failed = None
    for i, cmd in enumerate(cmds):
        full = [cli, *cmd[1:]]
        code, out, err = run(ctx, full, UPDATE_TIMEOUT)
        note_run(r, full, code, out, err)
        if code != 0 and i == len(cmds) - 1:
            failed = tail(err or out, 200)
    r["now"] = manifest_version(folder)
    if newer(r["now"], r["installed"]):
        r["status"] = "updated"
        r["changed"].append(f"плагин {PLUGIN}@{mp}: {r['installed']} → {r['now']} (copilot plugin update)")
        r["next_step"] = f"Новая версия {r['now']} на диске. {reload_step}"
    elif failed:
        r["status"], r["next_step"] = "error", f"`copilot plugin update {PLUGIN}` не прошёл: {failed}"
    else:
        r["status"] = "up_to_date"


# ---- Cursor ---------------------------------------------------------------------------------------------------------

def cursor(ctx, r):
    base = ctx.home / ".cursor/plugins"
    local = base / "local" / PLUGIN
    if manifest_version(local):
        cursor_local(ctx, r, local)
        return
    installs = [d for d in sorted(base.glob(f"*/*/{PLUGIN}/*"))
                if d.is_dir() and d.parents[2].name != "local" and manifest_version(d)]
    if not installs:
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=f"В Cursor плагин Seller AI не найден. Команде: Dashboard → Plugins → Import from Repo, адрес "
                           f"https://github.com/{REPO_SLUG}, включить Auto Refresh; без командного маркетплейса — "
                           f"`git clone https://github.com/{REPO_SLUG} ~/.cursor/plugins/local/{PLUGIN}`.")
        return
    folder = mine_first(ctx, installs) or max(installs, key=lambda d: mtime(d) or 0)
    r["installed"] = manifest_version(folder)
    r["auto_update"] = "on"
    note(r, f"копия из маркетплейса {folder.parents[1].name}: {tilde(ctx, folder)}")
    note(r, "клиент Cursor забирает новую версию при запуске и при фокусе окна; индекс командного маркетплейса "
            "обновляет Auto Refresh администратора (Dashboard → Settings → Plugins) — с этой машины его не видно")
    mark = mtime(folder)
    note_mark(ctx, r, "установка этой копии", mark)
    if skip(ctx, mark):
        r["status"] = "skipped"
        return
    commit = folder.name.lower()
    remote = git_remote_head(ctx, url=ctx.env.get("SELLER_AI_PLUGIN_REPO") or REPO)
    step = ("В Cursor новая версия: перезапусти Cursor или переключись в его окно — клиент заберёт её сам. Версия не "
            "меняется — администратор команды нажимает Refresh или включает Auto Refresh (нужно Cursor GitHub App). "
            "Плагин, добавленный через /add-plugin, закреплён на первом коммите — удали его и поставь из маркетплейса "
            "команды.")
    if remote and re.fullmatch(r"[0-9a-f]{7,40}", commit):
        if remote.startswith(commit) or commit.startswith(remote):
            r["status"] = "up_to_date"
        else:
            note(r, f"копия на коммите {commit[:12]}, в маркетплейсе — {remote[:12]}")
            r["status"], r["next_step"] = "needs_user", step
        return
    note(r, "не сверить с маркетплейсом: " + ("git ls-remote не ответил" if not remote
                                              else f"папка «{folder.name}» — не коммит"))
    r["status"], r["next_step"] = "error", step


def cursor_local(ctx, r, local):
    r["installed"] = manifest_version(local)
    r["auto_update"] = "unavailable"
    note(r, f"своя копия {tilde(ctx, local)}: автообновления у неё нет (его даёт командный маркетплейс с Auto Refresh)")
    mark = mtime(local / ".git/FETCH_HEAD")
    note_mark(ctx, r, "последнее обновление клона (.git/FETCH_HEAD)", mark)
    if skip(ctx, mark):
        r["status"] = "skipped"
        return
    reload_step = "В Cursor: палитра команд → «Developer: Reload Window» (или перезапуск)."
    if not (local / ".git").exists():
        r["status"] = "needs_user"
        r["next_step"] = (f"Копия {tilde(ctx, local)} — не клон git и сама не обновится: замени её клоном "
                          f"`git clone https://github.com/{REPO_SLUG} ~/.cursor/plugins/local/{PLUGIN}`. {reload_step}")
        return
    if ctx.dry_run:
        if git_moved(ctx, local) is False:
            r["status"] = "up_to_date"
            return
        would(r, shown(["git", "-C", str(local), "pull", "--ff-only"]))
        planned(r)
        return
    if not git_pull(ctx, r, local):
        return
    r["now"] = manifest_version(local)
    if newer(r["now"], r["installed"]):
        r["status"] = "updated"
        r["changed"].append(f"плагин {PLUGIN} ({tilde(ctx, local)}): {r['installed']} → {r['now']} (git pull)")
        r["next_step"] = f"Новая версия {r['now']} на диске. {reload_step}"
    else:
        r["status"] = "up_to_date"


# ---- Hermes ---------------------------------------------------------------------------------------------------------

def yaml_block(path, block):
    """Простые значения верхнего уровня блока YAML (plugins: …): чтение без парсера — в stdlib его нет."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out, inside, indent = {}, False, None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            if inside:
                break
            inside = re.match(rf"^{re.escape(block)}:\s*(#.*)?$", line) is not None
            continue
        if inside:
            width = len(line) - len(line.lstrip())
            indent = width if indent is None else indent
            m = re.match(r"^\s*([A-Za-z_][\w-]*):\s*(.*?)\s*(#.*)?$", line)
            if width == indent and m:
                out[m.group(1)] = m.group(2).strip("'\"")
    return out


def hermes_auto(ctx, r, cli, config):
    """plugins.auto_apply: true — gateway раз в auto_update_check_hours ставит обновления сам. Новые Hermes включают это
    командой `hermes plugins auto-update on`; YAML скрипт сам не правит."""
    block = yaml_block(config, "plugins") or {}
    hours = block.get("auto_update_check_hours", "24")
    stopped = hours in ("0", "0.0")
    if truthy(block.get("auto_apply")) and not stopped:
        r["auto_update"] = "on"
        note(r, f"plugins.auto_apply: true — обновления ставит запущенный `hermes gateway` раз в {hours} ч, кроме "
                "добавляющих инструменты, хуки или права")
        return True
    if stopped:
        note(r, "plugins.auto_update_check_hours: 0 — проверка обновлений gateway выключена")
    code, out, _ = run(ctx, [cli, "plugins", "--help"])
    if code == 0 and re.search(r"\bauto-update\b", out) and not stopped:
        cmd = [cli, "plugins", "auto-update", "on"]
        if ctx.dry_run:
            r["auto_update"] = "needs_user"
            would(r, shown(cmd))
            return False
        code, out, err = run(ctx, cmd)
        note_run(r, cmd, code, out, err)
        if code == 0 and truthy((yaml_block(config, "plugins") or {}).get("auto_apply")):
            r["auto_update"] = "enabled_now"
            r["changed"].append(f"{tilde(ctx, config)}: plugins.auto_apply: {block.get('auto_apply', 'нет')} → true "
                                "(hermes plugins auto-update on)")
            return True
    r["auto_update"] = "needs_user"
    note(r, "этот Hermes не включает автообновление командой, а YAML скрипт не правит")
    r["_auto"] = (f"Автообновление Hermes: в {tilde(ctx, config)} в блоке `plugins:` — строка `auto_apply: true`"
                  + (" и `auto_update_check_hours: 24` вместо 0" if stopped else "")
                  + "; тогда запущенный `hermes gateway` раз в сутки ставит обновления сам (кроме добавляющих "
                    "инструменты, хуки или права).")
    return False


def hermes(ctx, r):
    hh = Path(ctx.env.get("HERMES_HOME") or ctx.home / ".hermes")
    pdir = hh / "plugins" / PLUGIN
    cli = which(ctx, "hermes")
    r["installed"] = manifest_version(pdir)
    if r["installed"] is None:
        r.update(status="needs_user", auto_update="needs_user" if cli is None else "unavailable")
        r["next_step"] = (f"Ни команды hermes, ни папки плагина здесь нет — терминал агента, видимо, не на машине Hermes: "
                          f"обнови из чата Hermes — `/plugins update {PLUGIN}`; автообновление — `plugins.auto_apply: "
                          "true` в ~/.hermes/config.yaml на машине Hermes." if cli is None else
                          f"В Hermes плагин Seller AI не установлен: `hermes plugins install {REPO_SLUG}`, `hermes "
                          f"plugins enable {PLUGIN}`.")
        return
    note(r, f"установка Hermes: {tilde(ctx, pdir)}")
    if cli is None:
        block = yaml_block(hh / "config.yaml", "plugins") or {}
        r.update(status="needs_user", auto_update="on" if truthy(block.get("auto_apply")) else "needs_user",
                 next_step=f"Команды hermes нет в PATH: обнови из чата Hermes — `/plugins update {PLUGIN}`.")
        if r["auto_update"] == "needs_user":
            r["_auto"] = (f"Автообновление Hermes: в {tilde(ctx, hh / 'config.yaml')} в блоке `plugins:` — строка "
                          "`auto_apply: true`.")
        return
    meta = load_json(hh / "plugins/.install-metadata.json")
    row = meta.get(PLUGIN) if isinstance(meta, dict) and isinstance(meta.get(PLUGIN), dict) else {}
    if row.get("pinned"):
        r.update(status="needs_user", auto_update="unavailable",
                 next_step=f"Плагин закреплён на ревизии {str(row.get('revision', ''))[:12]} — обновления не придут. "
                           f"Переставь без закрепления: `hermes plugins install {row.get('source') or REPO} --force`.")
        return
    config = hh / "config.yaml"
    auto = hermes_auto(ctx, r, cli, config)
    mark, what = mtime(pdir / ".git/FETCH_HEAD"), "последнее обновление клона (.git/FETCH_HEAD)"
    if auto:  # с auto_apply и проверка gateway ставит обновления сама
        try:
            gate = float((hh / "plugin-update-checks/last-run").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            gate = None
        if gate and (mark is None or gate > mark):
            mark, what = gate, "последняя проверка gateway (plugin-update-checks/last-run)"
    note_mark(ctx, r, what, mark)
    reload_step = (f"Новые навыки — в новых сессиях Hermes; открытые чаты обновит `/plugins update {PLUGIN}` в самом "
                   "чате.")
    if skip(ctx, mark):
        r["status"] = "skipped"
        return
    code, out, _ = run(ctx, [cli, "plugins", "update", "--help"])
    cmd = [cli, "plugins", "update", PLUGIN] + (["--yes"] if code == 0 and "--yes" in out else [])
    if ctx.dry_run:
        check_cmd = [cli, "plugins", "check-updates", "--json"]
        code, out, err = run(ctx, check_cmd)
        note_run(r, check_cmd, code, out, err)
        try:
            rows = json.loads(out) if code == 0 else None
        except ValueError:
            rows = None
        row = next((x for x in rows if isinstance(x, dict) and x.get("name") == PLUGIN), None) \
            if isinstance(rows, list) else None
        if row and row.get("update_available") is False:
            r["status"] = "up_to_date"
            return
        would(r, shown(cmd))
        planned(r)
        return
    code, out, err = run(ctx, cmd, UPDATE_TIMEOUT)
    note_run(r, cmd, code, out, err)
    r["now"] = manifest_version(pdir)
    if newer(r["now"], r["installed"]):
        r["status"] = "updated"
        r["changed"].append(f"плагин {PLUGIN} ({tilde(ctx, pdir)}): {r['installed']} → {r['now']} (hermes plugins update)")
        r["next_step"] = f"Новая версия {r['now']} на диске. {reload_step}"
    elif code != 0:
        r["status"] = "error"
        r["next_step"] = f"`{shown(cmd)}` не прошёл: {tail(err or out, 200)}. Из чата Hermes — `/plugins update {PLUGIN}`."
    else:
        r["status"] = "up_to_date"


# ---- OpenCode -------------------------------------------------------------------------------------------------------

def is_clone(path):
    return (manifest(path) or {}).get("name") == PLUGIN and (Path(path) / "skills").is_dir()


def opencode_dir(ctx):
    return Path(ctx.env.get("XDG_CONFIG_HOME") or ctx.home / ".config") / "opencode"


def opencode_clones(ctx):
    """Клоны плагина, из которых OpenCode берёт навыки: ссылки в ~/.config/opencode/skills/, массив skills в
    opencode.json, папка по умолчанию. Клон, где лежит сам скрипт, сюда не входит, пока OpenCode на него не ссылается:
    рабочую копию разработчика скрипт не трогает."""
    cfg = opencode_dir(ctx)
    found = []
    skills = cfg / "skills"
    if skills.is_dir():
        found += [Path(os.path.realpath(p)).parent.parent for p in sorted(skills.iterdir()) if p.is_symlink()]
    for name in ("opencode.json", "opencode.jsonc"):
        data = load_json(cfg / name)
        for entry in (data.get("skills") or []) if isinstance(data, dict) else []:
            if isinstance(entry, str) and not re.match(r"^[a-z]+://", entry):
                p = Path(str(ctx.home) + entry[1:]) if entry.startswith("~") else Path(entry)
                found.append(p.parent if p.name == "skills" else p)
    found.append(cfg / "seller-ai-plugin")
    seen = []
    for p in found:
        if is_clone(p) and os.path.realpath(p) not in {os.path.realpath(s) for s in seen}:
            seen.append(p)
    return seen


def opencode_links(ctx, r, skills_dir, clone):
    """Навыки подключены ссылками в этот клон: новые навыки — новой ссылкой, ссылки на удалённые — убрать."""
    if not skills_dir.is_dir():
        return
    src = Path(clone) / "skills"
    links = [p for p in sorted(skills_dir.iterdir()) if p.is_symlink() and is_under(os.path.realpath(p), src)]
    if not links:
        return
    for skill in sorted(p for p in src.iterdir() if (p / "SKILL.md").is_file()):
        dest = skills_dir / skill.name
        if not dest.exists() and not dest.is_symlink():
            change(ctx, r, f"{tilde(ctx, dest)} → {tilde(ctx, skill)}: ссылка на новый навык")
            if not ctx.dry_run:
                dest.symlink_to(skill, target_is_directory=True)
    for link in links:
        if not link.exists():
            change(ctx, r, f"{tilde(ctx, link)}: ссылка на навык, которого больше нет в плагине, убрана")
            if not ctx.dry_run:
                link.unlink()


def opencode(ctx, r):
    clones = opencode_clones(ctx)
    r["auto_update"] = "unavailable"
    if not clones:
        r["status"] = "needs_user"
        r["next_step"] = (f"Клон плагина для OpenCode не найден: `git clone https://github.com/{REPO_SLUG} "
                          f"~/.config/opencode/{PLUGIN}-plugin` и в ~/.config/opencode/opencode.json — "
                          f"`\"skills\": [\"~/.config/opencode/{PLUGIN}-plugin/skills\"]`.")
        return
    clone, skills_dir = mine_first(ctx, clones) or clones[0], opencode_dir(ctx) / "skills"
    r["installed"] = manifest_version(clone)
    note(r, f"клон плагина: {tilde(ctx, clone)}; у OpenCode нет автообновления — его заменяет эта проверка раз в 30 "
            "минут работы")
    mark = mtime(Path(clone) / ".git/FETCH_HEAD")
    note_mark(ctx, r, "последнее обновление клона (.git/FETCH_HEAD)", mark)
    reload_step = ("OpenCode собирает список навыков на каждом шаге модели: новые навыки видны сразу, а навык, "
                   "прочитанный в этой сессии раньше, — перечитай или начни новую сессию.")
    if skip(ctx, mark):
        r["status"] = "skipped"
        return
    if not (Path(clone) / ".git").exists():
        r["status"] = "needs_user"
        r["next_step"] = (f"{tilde(ctx, clone)} — не клон git и сам не обновится: замени его клоном "
                          f"`git clone https://github.com/{REPO_SLUG}`.")
        return
    if ctx.dry_run:
        if git_moved(ctx, clone) is False:
            r["status"] = "up_to_date"
            opencode_links(ctx, r, skills_dir, clone)
            return
        would(r, shown(["git", "-C", str(clone), "pull", "--ff-only"]))
        would(r, f"затем ссылки в {tilde(ctx, skills_dir)}: на новые навыки — добавить, на удалённые — убрать")
        planned(r)
        return
    if not git_pull(ctx, r, clone):
        return
    r["now"] = manifest_version(clone)
    opencode_links(ctx, r, skills_dir, clone)
    if newer(r["now"], r["installed"]):
        r["status"] = "updated"
        r["changed"].insert(0, f"плагин {PLUGIN} ({tilde(ctx, clone)}): {r['installed']} → {r['now']} (git pull)")
        r["next_step"] = f"Новая версия {r['now']} на диске. {reload_step}"
    else:
        r["status"] = "up_to_date"


# ---- прочие ---------------------------------------------------------------------------------------------------------

def mcp_skills(ctx, r):
    r.update(status="up_to_date", auto_update="on")
    note(r, "навыки отдаёт MCP-сервер Seller AI (расширение Skills: skills/list) — локально обновлять нечего; клиент "
            "перечитывает их при запуске или переподключении сервера")


HANDLERS = {"claude-code": claude_code, "claude-app": claude_app, "codex": codex, "vscode": vscode,
            "copilot-cli": copilot_cli, "cursor": cursor, "hermes": hermes, "opencode": opencode,
            "mcp-skills": mcp_skills}


# ---- какой харнесс --------------------------------------------------------------------------------------------------

def install_areas(ctx):
    """Папки установки плагина у харнессов: путь этого файла внутри одной из них — харнесс этой сессии."""
    cdir, proot = claude_dirs(ctx)
    return [(proot, "claude-code"), (cdir / "skills", "claude-code"),
            (Path(ctx.env.get("CODEX_HOME") or ctx.home / ".codex") / "plugins", "codex"),
            (Path(ctx.env.get("COPILOT_HOME") or ctx.home / ".copilot") / "installed-plugins", "copilot-cli"),
            (ctx.home / ".vscode/agent-plugins", "vscode"), (ctx.home / ".vscode-insiders/agent-plugins", "vscode"),
            (ctx.home / ".cursor/plugins", "cursor"),
            (Path(ctx.env.get("HERMES_HOME") or ctx.home / ".hermes") / "plugins", "hermes"),
            (ctx.home / ".hermes", "hermes"), (opencode_dir(ctx), "opencode")]


def by_env(env):
    agent = str(env.get("AI_AGENT", "")).lower()
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_ENTRYPOINT") or agent.startswith("claude"):
        return "claude-code"
    if env.get("CODEX_SANDBOX") or env.get("CODEX_SANDBOX_NETWORK_DISABLED") or agent.startswith("codex"):
        return "codex"
    if env.get("CURSOR_TRACE_ID") or env.get("CURSOR_AGENT") or agent.startswith("cursor"):
        return "cursor"
    if env.get("OPENCODE") or env.get("OPENCODE_CLIENT") or agent.startswith("opencode"):
        return "opencode"
    if agent.startswith("hermes"):
        return "hermes"
    if agent.startswith("copilot") or agent.startswith("github-copilot"):
        return "copilot-cli"
    if env.get("TERM_PROGRAM") == "vscode":
        return "vscode"
    return None


def installed_in(ctx):
    """Харнессы, где на диске есть копия seller-ai."""
    cdir, proot = claude_dirs(ctx)
    out = []
    data = load_json(proot / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if (isinstance(plugins, dict) and any(k.startswith(PLUGIN + "@") for k in plugins)) \
            or any(proot.glob(f"synced/*/{PLUGIN}")):
        out.append("claude-code")
    if codex_installs(codex_store(ctx)):
        out.append("codex")
    if any((Path(ctx.env.get("COPILOT_HOME") or ctx.home / ".copilot") / "installed-plugins").glob(f"*/{PLUGIN}")):
        out.append("copilot-cli")
    if any((load_json(m) or {}).get("name") == PLUGIN for b in (".vscode", ".vscode-insiders")
           for m in (ctx.home / b / "agent-plugins").glob("*/*/*/plugin.json")):
        out.append("vscode")
    if manifest_version(ctx.home / ".cursor/plugins/local" / PLUGIN) \
            or any((ctx.home / ".cursor/plugins").glob(f"*/*/{PLUGIN}/*")):
        out.append("cursor")
    if manifest_version(Path(ctx.env.get("HERMES_HOME") or ctx.home / ".hermes") / "plugins" / PLUGIN):
        out.append("hermes")
    if opencode_clones(ctx):
        out.append("opencode")
    return out


def detect(ctx, flag):
    """(харнесс, как узнан): флаг; путь этого файла; клон, из которого OpenCode берёт навыки; окружение; единственная
    установленная копия."""
    if flag:
        name = ALIASES.get(flag.strip().lower(), flag.strip().lower())
        return (name, "--harness") if name in HANDLERS else (None, f"--harness {flag}: такого харнесса нет")
    paths = [ctx.script, Path(os.path.realpath(ctx.script))]
    for root, harness in install_areas(ctx):
        if any(is_under(p, root) for p in paths):
            return harness, f"скрипт лежит в {tilde(ctx, root)}"
    linked = opencode_clones(ctx)
    if any(is_under(root, c) for root in script_roots(ctx) for c in linked):
        return "opencode", "навыки OpenCode подключены из клона, где лежит скрипт"
    harness = by_env(ctx.env)
    if harness:
        return harness, "окружение"
    found = installed_in(ctx)
    if len(found) == 1:
        return found[0], "единственная установленная копия плагина"
    return None, ("не узнал харнесс; копии плагина есть в: " + ", ".join(found) if found
                  else "не узнал харнесс: ни пути, ни окружения, ни установленных копий")


def check(ctx, flag=None):
    harness, why = detect(ctx, flag)
    r = {"harness": harness, "installed": None, "now": None, "status": None, "auto_update": None, "changed": [],
         "next_step": None, "notes": []}
    if ctx.dry_run:
        r["dry_run"], r["would"] = True, []
    if harness is None:
        r.update(status="unknown_harness", auto_update="needs_user",
                 next_step="Запусти снова с --harness: " + ", ".join(HANDLERS) + " (таблица харнессов — в SKILL.md).")
        note(r, why)
        return r
    note(r, f"харнесс {harness}: {why}; особенности — {HARNESSES[harness]}")
    try:
        HANDLERS[harness](ctx, r)
    except Exception as e:  # ответ — всегда одна строка JSON, не трассировка
        r["status"] = "error"
        note(r, f"сбой проверки: {type(e).__name__}: {e}")
    auto_step = r.pop("_auto", None)
    if r["auto_update"] in ("needs_user", "unavailable") and auto_step:
        r["next_step"] = f"{r['next_step']} {auto_step}" if r["next_step"] else auto_step
    if ctx.dry_run and r["would"] and not r["next_step"]:
        r["next_step"] = "Без --dry-run скрипт выполнит: " + "; ".join(r["would"])
    r["now"] = r["now"] or r["installed"]
    r["auto_update"] = r["auto_update"] or "unavailable"
    r["status"] = r["status"] or "error"
    return r


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", help="харнесс, если он узнан не тот: " + ", ".join(HANDLERS))
    parser.add_argument("--force", action="store_true", help="проверить, даже если харнесс обновлялся < 30 минут назад")
    parser.add_argument("--dry-run", action="store_true", help="ничего не менять, показать план (would)")
    args = parser.parse_args(argv)
    r = check(Ctx(os.environ, time.time(), dry_run=args.dry_run, force=args.force), args.harness)
    print(json.dumps(r, ensure_ascii=False))
    return 1 if r["status"] == "error" else 2 if r["status"] == "unknown_harness" else 0


if __name__ == "__main__":
    sys.exit(main())
