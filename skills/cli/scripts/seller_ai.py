#!/usr/bin/env python3
"""Seller AI из терминала: клиент MCP-сервера Seller AI. Те же инструменты, права и проверки, что видит агент через MCP,
но вызов идёт из shell или скрипта, и печатается только ответ или его часть. Когда брать его вместо MCP — SKILL.md.

    python3 seller_ai.py login --code sal_…       вход по коду из подключения MCP (cli_codes_create): без браузера
    python3 seller_ai.py login [--token]          вход: браузер (OAuth, как у MCP) или токен «sai_…» со stdin
    python3 seller_ai.py status                   кто вошёл, какие магазины (токен не печатается)
    python3 seller_ai.py logout                   выйти и отозвать вход OAuth
    python3 seller_ai.py tools [ТЕКСТ] [--json]   инструменты входа: [r] чтение, [w] команда, [!] необратимая
    python3 seller_ai.py help ИНСТРУМЕНТ [--json]
    python3 seller_ai.py ИНСТРУМЕНТ [АРГУМЕНТЫ] [ФЛАГИ]      (то же самое: call ИНСТРУМЕНТ …)
    python3 seller_ai.py batch ФАЙЛ|- [--run МЕТКА] [ФЛАГИ] [--keep-going]

Аргументы: JSON-объект, @файл.json, - (JSON со stdin), ключ=строка, ключ:=JSON (число, true, массив, объект);
ключ с точкой (body.mode=add) — вложенное поле.
Флаги: --select ВЫРАЖЕНИЕ (a.b, items[].id, items[].{id,name}, supplies[0].cargo_ids, {operation,warnings}),
--wait (довести команду до конца: операция, этикетки), --timeout С (сколько ждать, 900), --yes (необратимая команда —
только после явного «да» пользователя), --dry-run (команды печатаются, а не отправляются; чтения идут; или
SELLER_AI_DRY_RUN=1), --out ПУТЬ (куда сохранить файл ответа; в batch — папка), --raw (весь ответ MCP).
batch: строки JSON {"id": …, "tool": …, "args": {…}, "yes": true} → по строке {"n", "id", "tool", "key", "ok",
"result" | "error", "warnings"}. С командами нужна метка запуска --run и свой id у каждой команды: ключ идемпотентности
— из метки, id, инструмента и аргументов, так что тот же запуск снова не отправит сделанное, а исправленная строка уйдёт
заново. Необратимая строка уходит только с "yes": true у неё самой. Первая ошибка останавливает, если нет --keep-going.

Вывод — JSON в stdout (ошибка — {"error", "exit", "detail"}), предупреждения сервера — «warning <код>: …» в stderr.
Коды выхода: 0 готово (или команда принята и идёт — без --wait); 1 ошибка инструмента или команда закончилась не
succeeded; 2 неверный вызов (ничего не отправлено); 3 нужно --yes (ничего не отправлено); 4 нужен вход; 5 сеть или
сервер; 6 повторить позже (retry_after); 7 команда ещё идёт после --wait.

Окружение: SELLER_AI_SERVER (по умолчанию https://seller.alven.studio), SELLER_AI_TOKEN (токен вместо входа),
SELLER_AI_CONFIG_DIR (где лежит вход; по умолчанию ~/.config/seller-ai, файл auth.json, права 600).

В Python: sys.path.insert(0, "<папка этого файла>"); import seller_ai; sa = seller_ai.Client();
sa.call("supplies_orders_get", store_id=..., order=...); sa.call(..., wait=True, idempotency_key=seller_ai.key(...));
seller_ai.outcome(ответ) — итог команды; sa.warnings — предупреждения последнего вызова; ошибки — seller_ai.CliError.
"""
from __future__ import annotations

import base64
import datetime
import getpass
import hashlib
import html
import http.client
import http.server
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

VERSION = "1.0.0"
DEFAULT_SERVER = "https://seller.alven.studio"
PROTOCOL = "2026-07-28"
# Команда ещё не закончилась (контракт: «Still open (accepted, waiting_quota, running, awaiting_confirmation,
# outcome_unknown)»; outcome_unknown Seller AI сверяет с Ozon сам).
OPEN = ("accepted", "waiting_quota", "running", "awaiting_confirmation", "outcome_unknown")
# «Нет ответа вовсе» (обрыв, 502–504 прокси): через полминуты тот же вызов — сервер ответит той же операцией.
LOST_ANSWER_PAUSE = 30.0
# Ответ на обновление входа потерян: тот же refresh-токен сервер ещё принимает в течение минуты.
REFRESH_RETRY_PAUSE = 3.0
LOGIN_WAIT = 600.0
LOOPBACK = ("127.0.0.1", "localhost", "::1")
USER_AGENT = f"seller-ai-cli/{VERSION} (python {sys.version_info[0]}.{sys.version_info[1]})"


class CliError(Exception):
    code = 1

    def __init__(self, message, body=None, code=None):
        super().__init__(message)
        self.body = body
        if code is not None:
            self.code = code


class ToolError(CliError):
    code = 1


class UsageError(CliError):
    code = 2


class NeedsYes(CliError):
    code = 3


class AuthError(CliError):
    code = 4


class NetworkError(CliError):
    code = 5


class RetryLater(CliError):
    code = 6


class StillOpen(CliError):
    code = 7


# ---- сеть ----------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправление не выполняется: токен уходит только туда, куда его послали."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = None


def _open():
    """Проверка сертификата как обычно; сборкам Python без своего набора корневых сертификатов (python.org на macOS)
    добавляется системный."""
    global _opener
    if _opener is None:
        ctx = ssl.create_default_context()
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(bundle):
                try:
                    ctx.load_verify_locations(bundle)
                except (OSError, ssl.SSLError):
                    pass
                break
        _opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    return _opener


def _http(method, url, body=None, headers=None, timeout=120):
    """(статус, тело, заголовки). Перенаправление и оборванный ответ — NetworkError."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    request.add_header("User-Agent", USER_AGENT)
    try:
        with _open().open(request, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise NetworkError(f"сервер перенаправил запрос ({e.code} → {e.headers.get('Location', '?')}): "
                               "не выполнено, вход и данные туда не отправлены")
        try:
            raw = e.read() or b""
        except (OSError, http.client.HTTPException):
            raw = b""
        return e.code, raw, dict(e.headers or {})
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise NetworkError(f"нет ответа от {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e) or type(e).__name__}")


def _json(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _post_form(url, data, timeout=120):
    status, raw, _ = _http("POST", url, urllib.parse.urlencode(data).encode(),
                           {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, timeout)
    got = _json(raw)
    return status, got if isinstance(got, dict) else {}


def _post_json(url, data):
    status, raw, _ = _http("POST", url, json.dumps(data).encode(),
                           {"Content-Type": "application/json", "Accept": "application/json"})
    got = _json(raw)
    return status, got if isinstance(got, dict) else {}


def _origin(server):
    parts = urllib.parse.urlsplit(server.strip().rstrip("/"))
    host = (parts.hostname or "").lower()
    if not parts.netloc or parts.scheme not in ("https", "http") or (parts.scheme == "http" and host not in LOOPBACK):
        raise UsageError(f"адрес сервера «{server}»: нужен https://хост (http — только для 127.0.0.1)")
    return f"{parts.scheme}://{parts.netloc}"


def _check_origin(url, server, what):
    parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    if f"{parts.scheme}://{parts.netloc}" != server:
        raise AuthError(f"{what} ведёт не на {server} ({parts.netloc or url}): прервано")
    return url


# ---- вход ------------------------------------------------------------------------------------------------------------

def _config_dir():
    path = os.environ.get("SELLER_AI_CONFIG_DIR")
    if not path:
        base = os.environ.get("XDG_CONFIG_HOME") or (os.environ.get("APPDATA") if os.name == "nt" else None) \
            or os.path.join(os.path.expanduser("~"), ".config")
        path = os.path.join(base, "seller-ai")
    return path


def _auth_file():
    return os.path.join(_config_dir(), "auth.json")


def _read_auth(strict=True):
    """Вход по серверам. Повреждённый файл: при чтении для вызова — ошибка, при записи — он откладывается в .bak."""
    path = _auth_file()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("servers", {}), dict):
            data.setdefault("servers", {})
            return data
    except FileNotFoundError:
        return {"servers": {}}
    except (OSError, ValueError):
        pass
    if strict:
        raise AuthError("файл входа повреждён: войдите заново (login)")
    try:
        os.replace(path, path + ".bak")
    except OSError:
        pass
    return {"servers": {}}


def _write_auth(data):
    os.makedirs(_config_dir(), mode=0o700, exist_ok=True)
    tmp = f"{_auth_file()}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _auth_file())


class _AuthLock:
    """Один процесс читает и обновляет вход за раз: обновлённый refresh-токен заменяет прежний."""

    def __enter__(self):
        os.makedirs(_config_dir(), mode=0o700, exist_ok=True)
        self.f = open(os.path.join(_config_dir(), "auth.lock"), "a+")
        try:
            import fcntl
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
            self.msvcrt = None
        except ImportError:
            import msvcrt
            self.msvcrt = msvcrt
            while True:
                try:
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        return self

    def __exit__(self, *exc):
        if self.msvcrt:
            try:
                self.f.seek(0)
                self.msvcrt.locking(self.f.fileno(), self.msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.f.close()


def _entry(server):
    with _AuthLock():
        return _read_auth()["servers"].get(server)


def _remember(server, entry):
    with _AuthLock():
        data = _read_auth(strict=False)
        old = data["servers"].get(server)
        data["servers"][server] = entry
        _write_auth(data)
    return old


def _forget(server):
    with _AuthLock():
        data = _read_auth(strict=False)
        old = data["servers"].pop(server, None)
        _write_auth(data)
    return old


def _refresh(server, entry):
    """Новый access-токен (под замком). Потерянный ответ — повтор тем же refresh-токеном, пока сервер его ещё берёт."""
    url = _check_origin(entry.get("token_endpoint"), server, "token_endpoint")
    form = {"grant_type": "refresh_token", "refresh_token": entry.get("refresh_token") or "",
            "client_id": entry.get("client_id") or "", "resource": entry.get("resource") or server + "/mcp"}
    # Короткий таймаут: все три попытки укладываются в минуту, пока сервер ещё берёт тот же refresh-токен.
    for attempt in range(3):
        try:
            status, tok = _post_form(url, form, timeout=15)
            break
        except NetworkError:
            if attempt == 2:
                raise
            time.sleep(REFRESH_RETRY_PAUSE)
    if status in (400, 401):
        raise AuthError("вход истёк или отозван: войдите заново (login)")
    if status == 429:
        raise RetryLater("сервер входа просит подождать: вход цел, повторите позже")
    if status != 200 or not isinstance(tok.get("access_token"), str):
        raise NetworkError(f"сервер входа ответил {status}: вход цел, повторите позже")
    entry = dict(entry, access_token=tok["access_token"],
                 refresh_token=tok.get("refresh_token") or entry.get("refresh_token"),
                 expires_at=time.time() + int(tok.get("expires_in") or 3600))
    data = _read_auth(strict=False)
    data["servers"][server] = entry
    _write_auth(data)
    return entry


def _metadata(server):
    status, raw, _ = _http("GET", server + "/.well-known/oauth-authorization-server", headers={"Accept": "application/json"})
    meta = _json(raw)
    if status != 200 or not isinstance(meta, dict):
        raise AuthError(f"сервер {server} не отдал настройки входа (OAuth): {status}")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not isinstance(meta.get(key), str):
            raise AuthError(f"в настройках входа сервера нет {key}")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        if key in meta:
            _check_origin(meta[key], server, key)
    return meta


def revoke(server, entry):
    """Отзыв входа OAuth на сервере (RFC 7009): True — сервер подтвердил. Токен «MCP и API» отзывают на сайте."""
    if not entry or entry.get("kind") != "oauth":
        return False
    try:
        url = _metadata(server).get("revocation_endpoint")
        if not url:
            return False
        done = True
        for hint in ("refresh_token", "access_token"):
            if entry.get(hint):
                status, _ = _post_form(url, {"token": entry[hint], "token_type_hint": hint,
                                             "client_id": entry.get("client_id", "")}, timeout=15)
                done = done and status == 200
        return done
    except CliError:
        return False


def oauth_login(server, open_browser=True):
    """Вход как у MCP-клиента: регистрация клиента, PKCE, согласие пользователя на сайте (магазины и права выбирает он)."""
    meta = _metadata(server)
    state = secrets.token_urlsafe(24)
    got = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
            ok = "code" in got and got.get("state") == state
            text = "Вход в Seller AI выполнен, окно можно закрыть." if ok else \
                "Вход не выполнен: " + html.escape(got.get("error_description") or got.get("error") or "нет кода")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<!doctype html><meta charset=utf-8><title>Seller AI</title><p>{text}</p>".encode())
            done.set()

    listener = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{listener.server_address[1]}/callback"
    try:
        status, client = _post_json(meta["registration_endpoint"], {
            "client_name": "Seller AI CLI", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        if status >= 300 or not isinstance(client.get("client_id"), str):
            raise AuthError(f"сервер не зарегистрировал клиента входа: {status} {client.get('error', '')}")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        resource = server + "/mcp"
        url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": "read write", "state": state,
            "resource": resource})
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        _err("Этот вход — только для CLI. Подключён MCP Seller AI — лучше войти его кодом (cli_codes_create, затем "
             "login --code); не подключён — подключить, иначе MCP останется без входа.\n"
             "Вход в Seller AI: в браузере выберите магазины и права и подтвердите. Если браузер не открылся, откройте "
             "ссылку:\n" + url + "\n")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        finished = done.wait(LOGIN_WAIT)
        listener.shutdown()
    finally:
        listener.server_close()
    if not finished:
        raise AuthError(f"вход не завершён за {int(LOGIN_WAIT // 60)} минут")
    if got.get("state") != state or "code" not in got:
        raise AuthError("вход отклонён: " + (got.get("error_description") or got.get("error") or "нет кода"))
    status, tok = _post_form(meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": client["client_id"], "code_verifier": verifier, "resource": resource})
    if status != 200 or not isinstance(tok.get("access_token"), str):
        raise AuthError(f"сервер не выдал доступ: {status} {tok.get('error', '')}")
    old = _remember(server, {"kind": "oauth", "client_id": client["client_id"], "token_endpoint": meta["token_endpoint"],
                             "resource": resource, "access_token": tok["access_token"],
                             "refresh_token": tok.get("refresh_token"),
                             "expires_at": time.time() + int(tok.get("expires_in") or 3600), "scope": tok.get("scope")})
    revoke(server, old)
    return {"signed_in": True, "server": server, "kind": "oauth", "scope": tok.get("scope")}


CLI_CODE_GRANT = "urn:seller-ai:params:oauth:grant-type:cli-code"


def code_login(server, code):
    """Вход по одноразовому коду из подключения MCP (инструмент cli_codes_create): без браузера и второго согласия, с
    магазинами и правами этого подключения. Код живёт 2 минуты и годится один раз."""
    meta = _metadata(server)
    if CLI_CODE_GRANT not in (meta.get("grant_types_supported") or []):
        raise AuthError("этот сервер ещё не принимает вход по коду: войдите через браузер (login)")
    if not re.fullmatch(r"sal_[A-Za-z0-9_\-]{16,200}", code or ""):
        raise UsageError("--code: нужен код sal_… из cli_codes_create")
    try:
        import socket
        host = socket.gethostname().split(".")[0][:40]
    except OSError:
        host = ""
    resource = server + "/mcp"
    # Обмен не повторяется: тот же код второй раз отзывает вход, который он уже мог выдать.
    try:
        status, tok = _post_form(meta["token_endpoint"], {"grant_type": CLI_CODE_GRANT, "code": code,
                                                          "resource": resource, "client_name": host}, timeout=30)
    except NetworkError as e:
        raise NetworkError(f"{e}: код уже мог сработать — этот не повторять, получите новый (cli_codes_create)")
    if status != 200 or not isinstance(tok.get("access_token"), str) or not isinstance(tok.get("client_id"), str):
        reason = tok.get("error_description") or tok.get("error") or f"ответ {status}"
        raise AuthError(f"код не принят ({reason}): получите новый — cli_codes_create — и сразу войдите им")
    old = _remember(server, {"kind": "oauth", "client_id": tok["client_id"], "token_endpoint": meta["token_endpoint"],
                             "resource": resource, "access_token": tok["access_token"],
                             "refresh_token": tok.get("refresh_token"),
                             "expires_at": time.time() + int(tok.get("expires_in") or 3600), "scope": tok.get("scope")})
    revoke(server, old)
    return {"signed_in": True, "server": server, "kind": "oauth", "via": "mcp_code", "scope": tok.get("scope")}


# ---- ответ инструмента -----------------------------------------------------------------------------------------------

class Answer:
    """Результат tools/call: тело (structuredContent или JSON текста), файлы, предупреждения сервера."""

    def __init__(self, result):
        self.raw = result
        self.is_error = bool(result.get("isError"))
        content = [c for c in (result.get("content") or []) if isinstance(c, dict)]
        self.texts = [c.get("text") or "" for c in content if c.get("type") == "text"]
        self.files = [c for c in content if c.get("type") in ("resource", "resource_link")]
        meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
        listed = [w for w in (meta.get("seller-ai/warnings") or []) if isinstance(w, dict)]
        self.warnings = [{"code": w.get("code"), "message": w.get("message")} for w in listed]
        structured = "structuredContent" in result
        first_is_body = structured or bool(self.files) or (bool(self.texts) and _json(self.texts[0].encode()) is not None)
        for text in (self.texts[1:] if first_is_body else self.texts):
            m = re.match(r"Warning ([\w-]+): (.*)", text, re.S)
            if m and not listed:
                self.warnings.append({"code": m.group(1), "message": m.group(2)})
            elif not m and self.files:
                self.warnings.append({"code": "note", "message": text})
        if structured:
            self.body = result["structuredContent"]
        elif self.texts and not self.files:
            parsed = _json(self.texts[0].encode())
            self.body = parsed if parsed is not None else {"text": "\n".join(self.texts)}
        else:
            self.body = None
        # Сервер кладёт предупреждения и внутрь ответа (для клиентов, что показывают модели только его). CLI печатает
        # их в stderr, поэтому из тела они убираются — в выводе их не будет дважды.
        if isinstance(self.body, dict) and isinstance(self.body.get("seller_ai_warnings"), list):
            self.body = dict(self.body)
            inside = [w for w in self.body.pop("seller_ai_warnings") if isinstance(w, dict)]
            if not listed:
                for w in inside:
                    found = {"code": w.get("code"), "message": w.get("message")}
                    if found not in self.warnings:
                        self.warnings.append(found)
        self.message = self.texts[0] if self.texts else ""


def _ops(body):
    """Операции ответа: [(место, операция)], место — 'operation', 'top' или индекс в operations."""
    found = []
    if not isinstance(body, dict):
        return found
    if isinstance(body.get("operation"), dict) and body["operation"].get("operation_id"):
        found.append(("operation", body["operation"]))
    elif isinstance(body.get("operation_id"), str):
        found.append(("top", body))
    for i, item in enumerate(body.get("operations") or []):
        if isinstance(item, dict) and item.get("operation_id"):
            found.append((i, item))
    return found


def _is_open(status):
    """Команда ещё идёт; операция без статуса — тоже: не закончившейся её назвать нельзя."""
    return not status or status in OPEN or status == "open"


def _put_op(body, where, op):
    if where == "operation":
        body["operation"] = op
    elif where == "top":
        body.update(op)
    else:
        body["operations"][where] = op


def _next_job(tool, args, body):
    """Файл ещё готовится (202 без операции): что спросить и через сколько секунд."""
    if not isinstance(body, dict):
        return None
    pause = max(1, int(body.get("retry_after") or 5)) if isinstance(body.get("retry_after"), (int, float)) else 5
    if body.get("task_id") and "retry_after" in body:
        return tool, dict(args, task_id=body["task_id"]), pause
    if body.get("job_id") and "retry_after" in body:
        if tool == "supplies_cargo_labels_create":
            return "supplies_cargo_labels_selected_download", {
                "store_id": args.get("store_id"), "order": args.get("order"), "supply": args.get("supply"),
                "job": body["job_id"]}, pause
        return tool, dict(args), pause
    if body.get("bundle_id") and body.get("state") == "working":
        return "supplies_cargo_labels_bundles_get", {"store_id": args.get("store_id"), "bundle": body["bundle_id"]}, pause
    return None


def outcome(body, read=False):
    """Итог команды: 'succeeded'; статус, которым она закончилась не так; открытый статус ('open' — без статуса), пока
    она идёт; None — в ответе нет ни операции, ни задания. read=True — ответ чтения: операции в нём — история объекта, не
    итог вызова; считается только набор этикеток."""
    if not isinstance(body, dict):
        return None
    statuses = [] if read else [op.get("status") for _, op in _ops(body)]
    if body.get("bundle_id") and body.get("state") in ("ready", "failed", "working"):
        statuses.append({"ready": "succeeded", "working": "running"}.get(body["state"], "failed"))
    if not statuses:
        return None
    bad = [s for s in statuses if not _is_open(s) and s != "succeeded"]
    if bad:
        return bad[0]
    still = [s or "open" for s in statuses if _is_open(s)]
    return still[0] if still else "succeeded"


def finished_badly(state):
    return state is not None and state != "succeeded" and not _is_open(state)


def key(*parts):
    """Ключ идемпотентности одного намерения: тот же набор частей — тот же ключ, повтор ничего не отправит дважды."""
    return "cli-" + hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _epoch(text):
    if not isinstance(text, str):
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", text.strip())
    if not m:
        return None
    zone = "+00:00" if m.group(3) in (None, "Z") else m.group(3)
    try:
        moment = datetime.datetime.fromisoformat(m.group(1) + ((m.group(2) or "")[:7]) + zone)
    except ValueError:
        return None
    return moment.timestamp()


def _http_date(text):
    """Время сервера из заголовка Date: сравнение с ним не зависит от часов этой машины."""
    try:
        import email.utils
        return email.utils.parsedate_to_datetime(text).timestamp() if text else None
    except (TypeError, ValueError, IndexError):
        return None


# ---- клиент ----------------------------------------------------------------------------------------------------------

def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class Client:
    """Вызовы инструментов MCP Seller AI. call() возвращает тело ответа (dict), предупреждения — в .warnings."""

    def __init__(self, server=None, token=None, dry_run=None, yes=False):
        self.server = _origin(server or os.environ.get("SELLER_AI_SERVER") or DEFAULT_SERVER)
        self.url = self.server + "/mcp"
        self._token = token or os.environ.get("SELLER_AI_TOKEN") or None
        self.dry_run = _truthy(os.environ.get("SELLER_AI_DRY_RUN")) if dry_run is None else bool(dry_run)
        self.yes = yes
        self.warnings = []
        self.planned = []
        self.last = None
        self.server_now = None
        self._tools = None
        self._id = 0

    # вход
    def _bearer(self, stale=None):
        if self._token:
            return self._token
        with _AuthLock():
            entry = _read_auth()["servers"].get(self.server)
            if not entry or not isinstance(entry.get("access_token"), str):
                raise AuthError("не выполнен вход: python3 seller_ai.py login (откроется браузер, магазины и права "
                                "выбирает пользователь)")
            if entry.get("kind") != "oauth":
                return entry["access_token"]
            # Другой процесс мог уже обновить вход, пока этот ждал: тогда новый токен годится как есть.
            fresh = time.time() < (entry.get("expires_at") or 0) - 300
            if fresh and entry["access_token"] != stale:
                return entry["access_token"]
            return _refresh(self.server, entry)["access_token"]

    def _rpc(self, method, params, name=None, token=None, repeatable=None):
        """Один запрос MCP. Ответа нет вовсе — тот же запрос ещё раз через полминуту, если его можно повторить:
        чтение, идемпотентная команда или команда с ключом идемпотентности (repeatable() решает по списку инструментов)."""
        self._id += 1
        params = dict(params)
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": PROTOCOL,
                           "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
                           "io.modelcontextprotocol/clientInfo": {"name": "seller-ai-cli", "version": VERSION}}
        payload = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        token = token or self._bearer()
        refreshed = repeated = False
        allowed = None
        while True:
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "Authorization": "Bearer " + token, "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method}
            if name:
                headers["Mcp-Name"] = name
            lost = None
            try:
                status, raw, got_headers = _http("POST", self.url, payload, headers)
            except NetworkError as e:
                if "перенаправил" in str(e):
                    raise
                lost = str(e)
            if lost is None:
                message = _json(raw)
                valid = isinstance(message, dict) and (isinstance(message.get("result"), dict)
                                                       or isinstance(message.get("error"), dict))
                if status == 401 and not valid:
                    if refreshed or self._token:
                        raise AuthError("вход не принят: войдите заново (login)")
                    refreshed = True
                    token = self._bearer(stale=token)
                    continue
                if status == 429 and not valid:
                    after = got_headers.get("Retry-After") or ""
                    raise RetryLater("сервер просит подождать (429)" + (f": через {after} с" if after.isdigit() else ""),
                                     body={"retry_after": int(after)} if after.isdigit() else None)
                if valid:
                    if "error" in message and isinstance(message["error"], dict):
                        err = message["error"]
                        code = 2 if err.get("code") in (-32601, -32602) else 5
                        raise CliError(f"MCP {err.get('code')}: {err.get('message')}", body=err.get("data"), code=code)
                    if status == 200:
                        self.server_now = _http_date(got_headers.get("Date") or got_headers.get("date"))
                        return message["result"]
                if status < 500 and valid is False and status != 200:
                    raise NetworkError(f"сервер ответил {status} без ответа MCP")
                if status == 200:
                    raise NetworkError("сервер ответил не по протоколу MCP")
                lost = f"сервер ответил {status} без ответа MCP"
            if allowed is None:
                allowed = True if repeatable is None else bool(repeatable())
            if allowed and not repeated:
                repeated = True
                time.sleep(LOST_ANSWER_PAUSE)
                continue
            tail = "" if allowed else \
                "; команда могла уйти — проверьте (operations_list, список объектов), прежде чем повторять"
            raise NetworkError(lost + tail)

    # инструменты
    def tools(self, repeat=True):
        if self._tools is None:
            listed, cursor, waited = [], None, False
            while True:
                try:
                    result = self._rpc("tools/list", {"cursor": cursor} if cursor else {},
                                       repeatable=None if repeat else (lambda: False))
                except RetryLater as e:
                    after = e.body.get("retry_after") if isinstance(e.body, dict) else None
                    if waited or not isinstance(after, (int, float)) or after > 60:
                        raise
                    waited = True
                    time.sleep(max(1, after))
                    continue
                listed += [t for t in (result.get("tools") or []) if isinstance(t, dict) and t.get("name")]
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self._tools = {t["name"]: t for t in listed}
        return self._tools

    def tool(self, name):
        found = self.tools().get(name)
        if found is None:
            close = [n for n in self.tools() if name.split("_")[0] in n][:8]
            hint = ("; похожие: " + ", ".join(close)) if close else ""
            raise UsageError(f"нет инструмента «{name}» у этого входа{hint} (список — tools)")
        return found

    def _annotations(self, name):
        return self.tool(name).get("annotations") or {}

    def read_only(self, name):
        return bool(self._annotations(name).get("readOnlyHint"))

    def takes_key(self, name):
        return "idempotency_key" in ((self.tool(name).get("inputSchema") or {}).get("properties") or {})

    def _repeatable(self, name):
        """Можно ли повторить вызов, ответ на который потерян: чтение, идемпотентная команда, команда с ключом."""
        try:
            found = self.tools(repeat=False).get(name) or {}
        except CliError:
            return False
        a = found.get("annotations") or {}
        props = (found.get("inputSchema") or {}).get("properties") or {}
        return bool(a.get("readOnlyHint") or a.get("idempotentHint") or "idempotency_key" in props)

    def _held_back(self, name):
        """--dry-run: не отправляется команда, а из чтений — те, что заводят в Ozon задание на файл (выгрузки этикеток,
        наборы и задания этикеток)."""
        a = self._annotations(name)
        return not a.get("readOnlyHint") or (bool(a.get("openWorldHint")) and name.endswith(("_download", "_create")))

    def state(self, tool, body):
        """Итог вызова для кода выхода: у чтения операции в ответе — история объекта, не итог (outcome read=True)."""
        found = outcome(body)
        return found if found is None else outcome(body, read=self.read_only(tool))

    # вызов
    def call(self, tool, args=None, *, wait=False, timeout=900, out=None, yes=None, **kwargs):
        if not isinstance(tool, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", tool):
            raise UsageError(f"имя инструмента «{tool}»: латиница, цифры и _")
        if args is not None and not isinstance(args, dict):
            raise UsageError("аргументы — объект")
        args = dict(args or {})
        args.update(kwargs)
        self.warnings = []
        if self.dry_run and self._held_back(tool):
            plan = {"dry_run": True, "tool": tool, "arguments": args}
            self.planned.append(plan)
            return plan
        if wait and "store_id" not in args and "store_id" in ((self.tool(tool).get("inputSchema") or {})
                                                              .get("properties") or {}):
            args["store_id"] = self._only_store()
        deadline = time.time() + timeout
        self.server_now = None
        answer = self._once(tool, args, deadline, wait, self.yes if yes is None else yes)
        if _ops(answer.body) and not self.read_only(tool):
            self._replayed(answer.body)
        if wait:
            answer = self._follow(tool, args, answer, deadline)
        if answer.files:
            return self._save(answer, out)
        return answer.body

    def _only_store(self):
        """--wait без store_id: магазин входа, если он один (сервер выбрал бы его сам); несколько — спросить до вызова."""
        items = (self._once("stores_list", {}, time.time() + 120, False, False, read=True).body or {}).get("items") or []
        if len(items) == 1 and isinstance(items[0], dict) and items[0].get("id"):
            return items[0]["id"]
        stores = [{"store_id": s.get("id"), "name": s.get("name")} for s in items if isinstance(s, dict)]
        raise UsageError("нужен store_id: " + "; ".join(f"{s['store_id']} — {s['name']}" for s in stores),
                         body={"stores": stores})

    def _note(self, warnings):
        for w in warnings:
            if w not in self.warnings:
                self.warnings.append(w)

    def _replayed(self, body):
        """Команда ответила операцией, созданной заметно раньше этого ответа (по часам сервера): тот же ключ уже был."""
        if self.server_now is None:
            return
        for _, op in _ops(body):
            created = _epoch(op.get("created_at"))
            if created is not None and created < self.server_now - 60:
                self._note([{"code": "replayed", "message": f"операция {op.get('operation_id')} от {op['created_at']}: "
                                                            "этот вызов уже был, заново ничего не отправлено"}])

    def _once(self, tool, args, deadline, wait, yes, read=False):
        """Один вызов инструмента с подтверждением и ожиданием retry_after. read=True — служебное чтение самого CLI
        (операция, магазины, готовность файла): его можно повторить без списка инструментов."""
        original = {"name": tool, "arguments": args}
        params, token, restarted = original, self._bearer(), False
        repeatable = (lambda: True) if read else (lambda: self._repeatable(tool))
        while True:
            try:
                result = self._rpc("tools/call", params, name=tool, token=token, repeatable=repeatable)
            except RetryLater as e:
                after = e.body.get("retry_after") if isinstance(e.body, dict) else None
                if wait and isinstance(after, (int, float)) and time.time() + after < deadline:
                    time.sleep(max(1, after))
                    params, token = original, self._bearer()
                    continue
                raise
            except CliError as e:
                if "requestState" in str(e) and params is not original and not restarted:
                    params, restarted, token = original, True, self._bearer()
                    continue
                raise
            kind = result.get("resultType", "complete")
            if kind == "input_required":
                asks = result.get("inputRequests") if isinstance(result.get("inputRequests"), dict) else {}
                if "confirm" in asks:
                    text = ((asks["confirm"] or {}).get("params") or {}).get("message") or f"{tool}: необратимая команда"
                    if not yes:
                        raise NeedsYes(text + " — ничего не отправлено", body={"needs_confirmation": text, "tool": tool,
                                                                               "arguments": args})
                    params = {"name": tool, "arguments": args, "requestState": result.get("requestState"),
                              "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}}}
                    continue
                if "store" in asks:
                    schema = ((asks["store"] or {}).get("params") or {}).get("requestedSchema") or {}
                    options = ((schema.get("properties") or {}).get("store_id") or {}).get("oneOf") or []
                    stores = [{"store_id": o.get("const"), "name": o.get("title")} for o in options if isinstance(o, dict)]
                    raise UsageError("нужен store_id: " + "; ".join(f"{s['store_id']} — {s['name']}" for s in stores),
                                     body={"stores": stores})
                raise CliError("сервер просит ввод, которого CLI не знает: " + ", ".join(asks), code=2)
            if kind not in ("complete", None):
                raise CliError(f"ответ вида «{kind}»: CLI его не ведёт", code=5)
            answer = Answer(result)
            self.last = result
            self._note(answer.warnings)
            if answer.is_error:
                body = answer.body if isinstance(answer.body, dict) else {}
                after = body.get("retry_after")
                if wait and isinstance(after, (int, float)) and time.time() + after < deadline:
                    time.sleep(max(1, after))
                    params, token = original, self._bearer()
                    continue
                if isinstance(after, (int, float)):
                    raise RetryLater(f"{answer.message} (повторить через {int(after)} с)", body=answer.body)
                raise ToolError(answer.message or "ошибка инструмента", body=answer.body)
            return answer

    def _follow(self, tool, args, answer, deadline):
        """--wait: операции команды — до конца; файл — пока готовится. Слежение прервалось (сеть, квота, срок) —
        StillOpen с ответом команды: она принята, её номер не теряется."""
        body = answer.body
        ops = [(w, op) for w, op in _ops(body) if _is_open(op.get("status"))]
        if ops and not self.read_only(tool) and args.get("store_id"):
            getter = "promotion_operations_get" if tool.startswith("promotion_") else "operations_get"
            for where, op in ops:
                try:
                    _put_op(body, where, self._wait_operation(getter, args["store_id"], op, deadline))
                except StillOpen as e:
                    _put_op(body, where, e.body)
                    raise StillOpen(f"{e} — ответ команды целиком в выводе", body=body)
                except CliError as e:
                    raise StillOpen(f"команда принята, слежение прервано ({e}): дальше — {getter}", body=body)
            return answer
        job = _next_job(tool, args, body)
        while job and not answer.files:
            name, job_args, pause = job
            if time.time() + pause > deadline:
                raise StillOpen("файл ещё готовится: следующий вызов — с task_id, job или bundle из этого ответа",
                                body=body)
            time.sleep(pause)
            try:
                answer = self._once(name, job_args, deadline, True, False, read=True)
            except CliError as e:
                raise StillOpen(f"файл ещё не получен ({e}): следующий вызов — с task_id, job или bundle из этого "
                                "ответа", body=body)
            body = answer.body
            job = _next_job(name, job_args, body)
        return answer

    def _wait_operation(self, getter, store, op, deadline):
        pause, last = 2.0, dict(op)
        while True:
            if time.time() + pause > deadline:
                raise StillOpen(f"операция {last.get('operation_id')} ещё идёт ({last.get('status')}): дальше — {getter}",
                                body=last)
            time.sleep(pause)
            got = self._once(getter, {"store_id": store, "operation": op["operation_id"]}, deadline, True, False,
                             read=True).body
            current = got.get("operation") if isinstance(got, dict) and isinstance(got.get("operation"), dict) else got
            if isinstance(current, dict) and current.get("operation_id"):
                last = current
            if not _is_open(last.get("status")):
                return last
            pause = min(pause * 1.6, 15.0)

    def _save(self, answer, out):
        saved = []
        for item in answer.files:
            if item.get("type") == "resource":
                res = item.get("resource") if isinstance(item.get("resource"), dict) else {}
                name = urllib.parse.unquote(str(res.get("uri") or "").rsplit("/", 1)[-1])
                try:
                    data = base64.b64decode(res["blob"]) if res.get("blob") else str(res.get("text") or "").encode()
                except (ValueError, TypeError):
                    raise NetworkError("файл ответа повреждён (base64)")
                mime = res.get("mimeType")
            else:
                name, mime = str(item.get("name") or ""), item.get("mimeType")
                status, data, _ = _http("GET", _check_link(item.get("uri"), self.server))
                if status != 200:
                    raise NetworkError(f"файл по ссылке не скачался: {status}")
            saved.append(_write_file(out, name, data, mime, several=len(answer.files) > 1))
        return saved[0] if len(saved) == 1 else {"files": saved}


def _check_link(uri, server):
    try:
        return _check_origin(uri, server, "ссылка на файл")
    except AuthError as e:
        raise NetworkError(str(e))


def _safe_name(name):
    name = os.path.basename(str(name).replace("\\", "/")).replace("\x00", "_").strip()
    if name in ("", ".", ".."):
        return "seller-ai-file"
    return "_" + name[1:] if name.startswith(".") else name


def _write_file(out, name, data, mime, several, overwrite=None):
    """Файл ответа на диск. В папке (--out ПАПКА, несколько файлов, без --out) имена не перезаписываются: «имя (2).pdf»."""
    name = _safe_name(name)
    if out and not several and not os.path.isdir(out) and overwrite is not False:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "wb") as f:
            f.write(data)
        return {"file": os.path.abspath(out), "bytes": len(data), "mime": mime}
    folder = out or "."
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(name)
    for n in range(1, 10000):
        path = os.path.join(folder, name if n == 1 else f"{stem} ({n}){ext}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return {"file": os.path.abspath(path), "bytes": len(data), "mime": mime}
    raise CliError("не нашлось свободного имени файла", code=5)


# ---- --select --------------------------------------------------------------------------------------------------------

_PART = re.compile(r"(\{[^}]*\}|[^.\[\]{}]*)((?:\[\d*\])*)")


def _split(expr):
    parts, depth, cur = [], 0, ""
    for ch in expr:
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if ch == "." and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def check_select(expr):
    """Разбор --select до вызова: ошибка в выражении не должна появиться после отправленной команды."""
    if not expr:
        return []
    parts = _split(expr)
    for part in parts:
        if not _PART.fullmatch(part):
            raise UsageError(f"--select: не понял «{part}» (примеры: a.b, items[].id, items[].{{id,name}}, a[0])")
    return parts


def _apply(value, parts):
    if not parts:
        return value
    part, rest = parts[0], parts[1:]
    m = _PART.fullmatch(part)
    head, brackets = m.group(1), re.findall(r"\[(\d*)\]", m.group(2))
    if head.startswith("{"):
        names = [n.strip() for n in head[1:-1].split(",") if n.strip()]
        value = {n: value.get(n) for n in names} if isinstance(value, dict) else None
    elif head:
        value = value.get(head) if isinstance(value, dict) else None
    for i, index in enumerate(brackets):
        if index == "":
            if not isinstance(value, list):
                return None
            tail = "".join(f"[{b}]" for b in brackets[i + 1:])
            return [_apply(v, ([tail] if tail else []) + rest) for v in value]
        value = value[int(index)] if isinstance(value, list) and int(index) < len(value) else None
    return _apply(value, rest)


def select(value, expr):
    """Часть ответа: a.b, items[].id, items[].{id,name}, supplies[0].cargo_ids, {operation,warnings}."""
    return _apply(value, check_select(expr)) if expr else value


# ---- командная строка ------------------------------------------------------------------------------------------------

FLAGS_WITH_VALUE = {"--select", "--timeout", "--out", "--server", "--run", "--code"}
FLAGS = {"--wait", "--yes", "--dry-run", "--raw", "--json", "--keep-going", "--token", "-h", "--help"}


def _options(tokens):
    opts, rest, i = {}, [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in FLAGS_WITH_VALUE:
            if i + 1 >= len(tokens):
                raise UsageError(f"{t}: нет значения")
            opts[t[2:]] = tokens[i + 1]
            i += 2
            continue
        if "=" in t and t.split("=", 1)[0] in FLAGS_WITH_VALUE:
            k, v = t.split("=", 1)
            opts[k[2:]] = v
        elif t in FLAGS:
            opts[t.lstrip("-")] = True
        elif t.startswith("--"):
            raise UsageError(f"неизвестный флаг {t} (справка — --help)")
        else:
            rest.append(t)
        i += 1
    if "timeout" in opts:
        try:
            opts["timeout"] = float(opts["timeout"])
        except ValueError:
            raise UsageError(f"--timeout {opts['timeout']}: нужно число секунд")
    return opts, rest


def _object(text, where):
    value = _json(text.encode() if isinstance(text, str) else text)
    if not isinstance(value, dict):
        raise UsageError(f"{where}: нужен JSON-объект")
    return value


def _read(path, where):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise UsageError(f"{where}: не прочитать {path} ({e.strerror or e})")


def _put(args, name, value):
    *path, last = name.split(".")
    node = args
    for p in path:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            raise UsageError(f"аргумент {name}: {p} уже не объект")
    node[last] = value


def parse_arguments(tokens, stdin=None):
    args = {}
    for t in tokens:
        if t == "-":
            args.update(_object((stdin or sys.stdin).read(), "stdin"))
        elif t.startswith("@"):
            args.update(_object(_read(t[1:], t), t))
        elif t.lstrip().startswith("{"):
            args.update(_object(t, "аргументы"))
        else:
            m = re.match(r"([A-Za-z_][\w.]*)(:=|=)(.*)$", t, re.S)
            if not m:
                raise UsageError(f"аргумент «{t}»: нужен JSON-объект, @файл, -, ключ=строка или ключ:=JSON")
            name, op, raw = m.groups()
            if op == ":=":
                value = _json(raw.encode())
                if value is None and raw.strip() != "null":
                    raise UsageError(f"аргумент {name}:= — не JSON: {raw}")
            else:
                value = raw
            _put(args, name, value)
    return args


def _write(stream, text):
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        stream.write(text)
        stream.flush()


def _print(value):
    _write(sys.stdout, json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _err(text):
    _write(sys.stderr, text)


def _warn(warnings):
    for w in warnings:
        _err(f"warning {w.get('code')}: {w.get('message')}\n")


def _shown(raw):
    """Весь ответ MCP для --raw, без содержимого файлов и без ссылок на них."""
    raw = json.loads(json.dumps(raw))
    for item in raw.get("content") or []:
        if not isinstance(item, dict):
            continue
        res = item.get("resource")
        if isinstance(res, dict) and res.get("blob"):
            res["blob"] = f"<{len(res['blob'])} символов base64>"
        if item.get("type") == "resource_link":
            item["uri"] = "<ссылка на файл, действует несколько минут — не показывается>"
    return raw


def _kind(tool):
    a = tool.get("annotations") or {}
    return "r" if a.get("readOnlyHint") else ("!" if a.get("destructiveHint") else "w")


def _first_line(text, limit=110):
    line = (text or "").strip().split("\n", 1)[0]
    return line if len(line) <= limit else line[:limit - 1] + "…"


def cmd_tools(client, opts, rest):
    needle = " ".join(rest).lower()
    found = [t for t in client.tools().values()
             if not needle or needle in t["name"].lower() or needle in (t.get("description") or "").lower()]
    if opts.get("json"):
        _print([{"name": t["name"], "kind": _kind(t), "summary": _first_line(t.get("description"))} for t in found])
    else:
        _write(sys.stdout, "".join(f"{t['name']} [{_kind(t)}] — {_first_line(t.get('description'))}\n"
                                   for t in sorted(found, key=lambda t: t["name"])))
        _err(f"{len(found)} из {len(client.tools())}: [r] чтение, [w] команда, [!] необратимая\n")
    return 0


def cmd_help(client, opts, rest):
    if not rest:
        _write(sys.stdout, __doc__)
        return 0
    tool = client.tool(rest[0])
    if opts.get("json"):
        _print(tool)
        return 0
    schema = tool.get("inputSchema") or {}
    required = set(schema.get("required") or [])
    lines = [f"{tool['name']} [{_kind(tool)}]", (tool.get("description") or "").strip(), "", "Аргументы:"]
    for name, prop in (schema.get("properties") or {}).items():
        prop = prop if isinstance(prop, dict) else {}
        kind = prop.get("type") or ("enum" if "enum" in prop else "object")
        kind = "/".join(kind) if isinstance(kind, list) else kind
        mark = ", обязательный" if name in required else ""
        lines.append(f"  {name} ({kind}{mark}) — {_first_line(prop.get('description'), 140)}")
    _write(sys.stdout, "\n".join(lines) + "\n")
    return 0


def cmd_status(client, opts):
    info = {"server": client.server}
    if client._token:
        info["kind"] = "SELLER_AI_TOKEN"
    else:
        entry = _entry(client.server)
        if not entry:
            info["signed_in"] = False
            _print(info)
            return 4
        info["kind"] = entry.get("kind")
        if entry.get("kind") == "oauth":
            info["access_expires_in_s"] = int((entry.get("expires_at") or 0) - time.time())
            info["refresh"] = bool(entry.get("refresh_token"))
    current = client.call("access_tokens_current_get")
    info["signed_in"] = True
    info["token"] = select(current, "token.{name,audience,status,expires_at}")
    info["stores"] = select(current, "stores[].{store_id,name,role}")
    _print(info)
    return 0


def cmd_login(client, opts):
    if opts.get("token"):
        if sys.stdin.isatty():
            token = getpass.getpass("Токен Seller AI из «Настройки → MCP и API» (sai_…): ").strip()
        else:
            token = sys.stdin.readline().strip()
        if not token:
            raise UsageError("пустой токен")
        Client(server=client.server, token=token).call("access_tokens_current_get")
        revoke(client.server, _remember(client.server, {"kind": "token", "access_token": token}))
        _print({"signed_in": True, "server": client.server, "kind": "token"})
        return 0
    if opts.get("code"):
        _print(code_login(client.server, opts["code"].strip()))
        return 0
    _print(oauth_login(client.server))
    return 0


def cmd_logout(client):
    old = _forget(client.server)
    revoked = revoke(client.server, old)
    if revoked:
        note = "вход OAuth отозван на сервере"
    elif old and old.get("kind") == "oauth":
        note = "здесь вход забыт, а отозвать его на сервере не удалось: отзовите на сайте — Настройки → MCP и API"
    elif old:
        note = "токен «MCP и API» здесь забыт; отзывают его на сайте: Настройки → MCP и API"
    else:
        note = "входа не было"
    _print({"signed_out": bool(old), "server": client.server, "note": note})
    return 0


def cmd_call(client, opts, tool, tokens):
    check_select(opts.get("select"))
    args = parse_arguments(tokens)
    result = client.call(tool, args, wait=bool(opts.get("wait")), timeout=opts.get("timeout") or 900,
                         out=opts.get("out"))
    _warn(client.warnings)
    if opts.get("raw") and client.last is not None:
        _print(_shown(client.last))
    else:
        _print(select(result, opts.get("select")))
    state = client.state(tool, result)
    if finished_badly(state):
        _err(f"error: команда закончилась статусом {state}\n")
        return 1
    return 0


def cmd_batch(client, opts, rest):
    check_select(opts.get("select"))
    if opts.get("yes"):
        raise UsageError("batch: «да» пользователя ставится у строки — \"yes\": true у каждой необратимой команды")
    if opts.get("out") and os.path.isfile(opts["out"]):
        raise UsageError("batch: --out — папка, а не файл (файлы строк не должны затирать друг друга)")
    if opts.get("out"):
        os.makedirs(opts["out"], exist_ok=True)
    source = rest[0] if rest else "-"
    text = sys.stdin.read() if source == "-" else _read(source, "batch")
    rows, problems = [], []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        n = len(rows) + 1
        item = _json(raw.encode())
        problem = None
        if not isinstance(item, dict):
            problem = f"строка {n}: нужен JSON-объект"
        elif not isinstance(item.get("tool"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", item["tool"]):
            problem = f"строка {n}: tool — имя инструмента"
        elif item.get("args") is not None and not isinstance(item.get("args"), dict):
            problem = f"строка {n}: args — объект"
        if problem:
            problems.append(problem)
        rows.append(item)
    if problems:
        raise UsageError("batch не начат: " + "; ".join(problems[:5]))
    # Ключ команды строки — (метка запуска, id, инструмент, аргументы): исправленная строка уйдёт заново, остальные —
    # нет. Поэтому у команд метка задаётся явно, а id есть у каждой и не повторяется.
    commands = [(n, item) for n, item in enumerate(rows, 1) if not client.read_only(item["tool"])]
    run = opts.get("run")
    if commands and not client.dry_run:
        if not run:
            raise UsageError("batch с командами: нужна метка запуска --run <метка> (например series-2026-11-08): тот же "
                             "запуск с той же меткой не отправит сделанное заново; новая просьба — новая метка")
        ids = [item.get("id") for _, item in commands]
        missing = [n for n, item in commands if item.get("id") in (None, "")]
        repeated_ids = sorted({str(i) for i in ids if i not in (None, "") and ids.count(i) > 1})
        if missing or repeated_ids:
            raise UsageError("batch не начат: у каждой команды свой id — " +
                             (f"нет id в строках {missing[:10]}" if missing else f"повторяются {repeated_ids[:10]}"))
        _err(f"batch: метка запуска {run}\n")
    failures, first_code = 0, 0
    for n, item in enumerate(rows, 1):
        args = dict(item.get("args") or {})
        row = {"n": n, "id": item.get("id"), "tool": item["tool"]}
        client.warnings = []
        code = 0
        try:
            if run and "idempotency_key" not in args and not client.dry_run and client.takes_key(item["tool"]):
                args["idempotency_key"] = key(run, item["id"], item["tool"], args)
            if args.get("idempotency_key"):
                row["key"] = args["idempotency_key"]
            result = client.call(item["tool"], args, wait=bool(opts.get("wait")), timeout=opts.get("timeout") or 900,
                                 out=opts.get("out") or ".", yes=item.get("yes") is True)
            state = client.state(item["tool"], result)
            row["ok"] = not finished_badly(state)
            row["result"] = select(result, opts.get("select"))
            if not row["ok"]:
                row["error"] = f"команда закончилась статусом {state}"
                code = 1
        except CliError as e:
            row.update(ok=False, error=str(e), exit=e.code)
            if e.body is not None:
                row["detail"] = e.body
            code = e.code
        except Exception as e:
            row.update(ok=False, error=f"{type(e).__name__}: {e}", exit=5)
            code = 5
        if client.warnings:
            row["warnings"] = client.warnings
        _print(row)
        if not row["ok"]:
            failures += 1
            first_code = first_code or code
            if not opts.get("keep-going"):
                break
    _err(f"batch: {len(rows)} строк, ошибок {failures}" + (f", метка запуска {run}" if run else "") + "\n")
    return first_code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    client = None
    try:
        opts, rest = _options(argv)
        if not rest or opts.get("help") or opts.get("h"):
            if rest and rest[0] not in ("help", "tools", "login", "logout", "status", "batch", "call"):
                rest = ["help", rest[0]]
            else:
                _write(sys.stdout, __doc__)
                return 0
        command, rest = rest[0], rest[1:]
        client = Client(server=opts.get("server"), dry_run=True if opts.get("dry-run") else None,
                        yes=bool(opts.get("yes")))
        if command == "login":
            return cmd_login(client, opts)
        if command == "logout":
            return cmd_logout(client)
        if command == "status":
            return cmd_status(client, opts)
        if command == "tools":
            return cmd_tools(client, opts, rest)
        if command == "help":
            return cmd_help(client, opts, rest)
        if command == "batch":
            return cmd_batch(client, opts, rest)
        if command == "call":
            if not rest:
                raise UsageError("call: нет имени инструмента")
            command, rest = rest[0], rest[1:]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", command):
            raise UsageError(f"«{command}» — не команда и не имя инструмента (справка — --help)")
        return cmd_call(client, opts, command, rest)
    except CliError as e:
        if client is not None and client.warnings:
            _warn(client.warnings)
        _print({"error": str(e), "exit": e.code, "detail": e.body})
        _err(f"error: {e}\n")
        return e.code
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if client is not None and client.warnings:
            _warn(client.warnings)
        text = f"{type(e).__name__}: {e}"
        _print({"error": text, "exit": 5, "detail": None})
        _err(f"error: {text}\n")
        return 5


if __name__ == "__main__":
    sys.exit(main())
