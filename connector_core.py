# SPDX-License-Identifier: GPL-2.0-or-later
"""What the connectors share: one passphrase, OAuth 2.1 for ChatGPT and Claude, the sign-in page.

Each connector (e.g. thoughts_connector.py) is its own process and its own OAuth
authorization server under its own path on the public host (CONNECTOR_PUBLIC_URL/<prefix>):

    <prefix>/mcp                         MCP endpoint (streamable HTTP), OAuth bearer tokens
    <prefix>/authorize|token|register|revoke   OAuth 2.1 (PKCE, dynamic client registration)
    <prefix>/login                       the passphrase page (OAuth consent and browser sign-in)

plus the discovery documents /.well-known/oauth-protected-resource<prefix>/mcp and
/.well-known/oauth-authorization-server<prefix> (and the openid-configuration spellings).

Access is one passphrase for all connectors (CONNECTOR_PASSPHRASE, an scrypt hash, set with
`<connector> set-passphrase`). Only redirect URIs of ChatGPT and Claude (web, or a loopback port for desktop apps) may register as OAuth
clients (CONNECTOR_EXTRA_REDIRECTS adds a regex, for tests). Clients and tokens (hashed) live in
the connector's state folder; `<connector> revoke-all` disconnects every client and browser.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

# httpx logs every request URL at INFO, and the media server's token is part of its URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

PUBLIC = os.environ.get("CONNECTOR_PUBLIC_URL", "https://thoughts.example.org").rstrip("/")
PASSPHRASE_FILE = Path(os.environ.get("CONNECTOR_PASSPHRASE", Path.home() / "connectors" / "passphrase.json"))

ACCESS_TTL = 24 * 3600
REFRESH_TTL = 180 * 24 * 3600
SESSION_TTL = 90 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 900
MAX_CLIENTS = 50

ALLOWED_REDIRECTS = [
    re.compile(r"^https://chatgpt\.com/[A-Za-z0-9_./-]*$"),
    re.compile(r"^https://chat\.openai\.com/[A-Za-z0-9_./-]*$"),
    re.compile(r"^https://platform\.openai\.com/[A-Za-z0-9_./-]*$"),
    re.compile(r"^https://claude\.(ai|com)/api/mcp/auth_callback$"),
    # Desktop clients (Codex, Claude Code, Claude Desktop) finish sign-in on a loopback port of the
    # computer the passphrase was typed on, so a code can only land there.
    re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}(/[A-Za-z0-9_./-]*)?$"),
]
if os.environ.get("CONNECTOR_EXTRA_REDIRECTS"):
    ALLOWED_REDIRECTS.append(re.compile(os.environ["CONNECTOR_EXTRA_REDIRECTS"]))


@dataclass
class Service:
    name: str        # "Listen"
    prefix: str      # "/listen"
    port: int
    state: Path      # oauth.json, secret.key
    what: str        # for the consent page: "your audiobooks and videos"
    public: str = PUBLIC
    passphrase_file: Path = PASSPHRASE_FILE

    @property
    def base(self) -> str:
        return self.public + self.prefix

    @property
    def resource(self) -> str:
        return self.base + "/mcp"

    @property
    def scope(self) -> str:
        return self.prefix.strip("/")


def log(message: str):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def write_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---- secrets: signing key and passphrase -------------------------------------------------------
class Keys:
    """HMAC key for signed links and browser sessions (state/secret.key; rotated by revoke-all)."""

    def __init__(self, state: Path):
        self.path = state / "secret.key"
        if not self.path.exists():
            write_atomic(self.path, secrets.token_bytes(32))
        self.key = self.path.read_bytes()

    def sign(self, *parts: str) -> str:
        return b64(hmac.new(self.key, "\n".join(parts).encode(), hashlib.sha256).digest())[:32]

    def check(self, signature: str, *parts: str) -> bool:
        return hmac.compare_digest(signature.encode(), self.sign(*parts).encode())


PASSPHRASE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no i/l/o/0/1: easy to type on a phone


def normalize_passphrase(text: str) -> str:
    return re.sub(r"[\s-]+", "", text).lower()


def new_passphrase() -> str:
    groups = ["".join(secrets.choice(PASSPHRASE_ALPHABET) for _ in range(4)) for _ in range(5)]
    return "-".join(groups)  # 20 symbols of 31: ~99 bits


def hash_passphrase(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(normalize_passphrase(passphrase).encode(), salt=salt, n=2**15, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=32)


def set_passphrase(path: Path, passphrase: str):
    salt = secrets.token_bytes(16)
    record = {"salt": salt.hex(), "hash": hash_passphrase(passphrase, salt).hex(), "set_at": int(time.time())}
    write_atomic(path, json.dumps(record).encode())


def check_passphrase(path: Path, passphrase: str) -> bool:
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    computed = hash_passphrase(passphrase, bytes.fromhex(record["salt"]))
    return hmac.compare_digest(computed, bytes.fromhex(record["hash"]))


class Throttle:
    """Wrong passphrases: 5 per address or 30 overall within 15 minutes lock the login for 15 minutes."""

    WINDOW, PER_ADDRESS, OVERALL = 900, 5, 30

    def __init__(self):
        self.failures: dict[str, list[float]] = {}

    def _recent(self, address: str | None = None) -> int:
        cutoff = time.time() - self.WINDOW
        for key in list(self.failures):
            self.failures[key] = [t for t in self.failures[key] if t > cutoff]
            if not self.failures[key]:
                del self.failures[key]
        if address is not None:
            return len(self.failures.get(address, []))
        return sum(len(v) for v in self.failures.values())

    def locked(self, address: str) -> bool:
        return self._recent(address) >= self.PER_ADDRESS or self._recent() >= self.OVERALL

    def fail(self, address: str):
        self.failures.setdefault(address, []).append(time.time())

    def succeed(self, address: str):
        self.failures.pop(address, None)


# ---- OAuth: authorization server with a passphrase page ----------------------------------------
def redirect_allowed(uri: str) -> bool:
    return any(pattern.match(uri) for pattern in ALLOWED_REDIRECTS)


def same_resource(a: str | None, b: str) -> bool:
    return a is not None and a.rstrip("/") == b.rstrip("/")


class Provider:
    """OAuthAuthorizationServerProvider: clients and hashed tokens in state/oauth.json, codes in memory."""

    def __init__(self, service: Service):
        self.service = service
        self.path = service.state / "oauth.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        for key in ("clients", "access", "refresh"):
            self.data.setdefault(key, {})
        self.pending: dict[str, dict] = {}  # login transaction -> client_id, params, created
        self.codes: dict[str, Any] = {}

    def save(self):
        now = time.time()
        for kind in ("access", "refresh"):
            self.data[kind] = {k: v for k, v in self.data[kind].items() if v["expires_at"] > now}
        write_atomic(self.path, json.dumps(self.data, indent=1).encode())

    # clients
    async def get_client(self, client_id: str):
        from mcp.shared.auth import OAuthClientInformationFull
        record = self.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    async def register_client(self, client_info):
        from mcp.server.auth.provider import RegistrationError
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(redirect_allowed(u) for u in uris):
            log(f"oauth: refused registration with redirect URIs {uris}")
            raise RegistrationError("invalid_redirect_uri", "only ChatGPT and Claude may connect")
        clients = self.data["clients"]
        if len(clients) >= MAX_CLIENTS:
            used = {v["client_id"] for v in [*self.data["access"].values(), *self.data["refresh"].values()]}
            unused = sorted((c for c in clients if c not in used), key=lambda c: clients[c].get("client_id_issued_at", 0))
            for client_id in unused[: len(clients) - MAX_CLIENTS + 1]:
                del clients[client_id]
        clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        self.save()
        log(f"oauth: registered client {client_info.client_name!r} -> {', '.join(urlparse(u).netloc for u in uris)}")

    # authorization: send the browser to the passphrase page
    async def authorize(self, client, params) -> str:
        from mcp.server.auth.provider import AuthorizeError
        if params.resource is not None and not same_resource(params.resource, self.service.resource):
            raise AuthorizeError("invalid_request", f"unknown resource {params.resource}")
        now = time.time()
        for txn in [t for t, p in self.pending.items() if now - p["created"] > LOGIN_TTL]:
            del self.pending[txn]
        if len(self.pending) > 200:
            raise AuthorizeError("temporarily_unavailable", "too many sign-ins in progress")
        txn = secrets.token_urlsafe(24)
        self.pending[txn] = {"client_id": client.client_id, "params": params, "created": now,
                             "client_name": client.client_name or "an app"}
        return f"{self.service.base}/login?{urlencode({'txn': txn})}"

    def approve(self, txn: str) -> str:
        """The passphrase was right: a code for the waiting client; returns the redirect back to it."""
        from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri
        pending = self.pending.pop(txn)
        params = pending["params"]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=[self.service.scope], expires_at=time.time() + CODE_TTL, client_id=pending["client_id"],
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.service.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code: str):
        code = self.codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id)

    def _issue(self, client_id: str):
        from mcp.shared.auth import OAuthToken
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.data["refresh"][digest(refresh)] = {"client_id": client_id, "expires_at": now + REFRESH_TTL}
        self.data["access"][digest(access)] = {"client_id": client_id, "expires_at": now + ACCESS_TTL,
                                               "refresh": digest(refresh)}
        self.save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, scope=self.service.scope,
                          refresh_token=refresh)

    async def load_refresh_token(self, client, refresh_token: str):
        from mcp.server.auth.provider import RefreshToken
        record = self.data["refresh"].get(digest(refresh_token))
        if record and record["client_id"] == client.client_id and record["expires_at"] > time.time():
            return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=[self.service.scope],
                                expires_at=record["expires_at"], resource=self.service.resource)
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self._drop(digest(refresh_token.token))
        return self._issue(client.client_id)

    async def load_access_token(self, token: str):
        from mcp.server.auth.provider import AccessToken
        record = self.data["access"].get(digest(token))
        if record and record["expires_at"] > time.time():
            return AccessToken(token=token, client_id=record["client_id"], scopes=[self.service.scope],
                               expires_at=record["expires_at"], resource=self.service.resource)
        return None

    async def revoke_token(self, token):
        key = digest(token.token)
        record = self.data["access"].get(key)
        self._drop(record["refresh"] if record else key)
        self.data["access"].pop(key, None)
        self.save()

    def _drop(self, refresh_key: str):
        """A refresh token and every access token issued with it."""
        self.data["refresh"].pop(refresh_key, None)
        for key in [k for k, v in self.data["access"].items() if v.get("refresh") == refresh_key]:
            del self.data["access"][key]
        self.save()


# ---- pages -------------------------------------------------------------------------------------
PAGE_STYLE = """
:root{color-scheme:light dark;--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--accent:#2f6f5e;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a19f97;--line:#33322e;--accent:#7cc4ad;--card:#1f1f1d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 16px}h2{font-size:15px;color:var(--muted);margin:20px 0 4px}
input{font:inherit;width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg)}
button{font:inherit;padding:12px 16px;border:0;border-radius:10px;background:var(--accent);color:var(--bg);margin-top:12px;width:100%}
.muted{color:var(--muted);font-size:14px}.error{color:#c0392b}
a{color:inherit;text-decoration:none}ul{list-style:none;padding:0;margin:0}
li a{display:flex;justify-content:space-between;gap:12px;padding:12px 4px;border-bottom:1px solid var(--line)}
.tag{color:var(--muted);font-size:13px;white-space:nowrap}
"""
PAGE_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                           "media-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"}


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{PAGE_STYLE}</style></head><body><main>{body}</main></body></html>")


def login_page(service: Service, prompt: str, hidden: dict[str, str], error: str = "") -> str:
    fields = "".join(f"<input type=hidden name={html.escape(k)} value='{html.escape(v, quote=True)}'>"
                     for k, v in hidden.items())
    message = f"<p class=error>{html.escape(error)}</p>" if error else ""
    return page(f"{service.name} sign-in", f"""<h1>{html.escape(service.name)}</h1><p>{prompt}</p>{message}
<form method=post action='{service.prefix}/login'>{fields}
<input name=passphrase type=password autocomplete=current-password autocapitalize=none
 placeholder=Passphrase autofocus required><button>Continue</button></form>""")


# ---- the app -----------------------------------------------------------------------------------
@dataclass
class Context:
    """What a connector's tools and routes need from the shared part."""
    service: Service
    mcp: Any
    keys: Keys
    provider: Provider
    throttle: Throttle
    players: "Players | None" = None

    def signed_in(self, request) -> bool:
        expires, _, signature = request.cookies.get(self.cookie, "").partition(".")
        return expires.isdigit() and int(expires) > time.time() and self.keys.check(signature, "session", expires)

    @property
    def cookie(self) -> str:
        return self.service.scope + "_session"

    def login_redirect(self, next_path: str):
        from starlette.responses import RedirectResponse
        return RedirectResponse(f"{self.service.prefix}/login?{urlencode({'next': next_path})}", status_code=303)


def build(service: Service, instructions: str) -> Context:
    """FastMCP with OAuth (passphrase page) mounted under service.prefix; add tools/routes, then app(ctx)."""
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

    service.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider = Provider(service)
    ctx = Context(service, None, Keys(service.state), provider, Throttle())
    prefix = service.prefix
    ctx.mcp = mcp = FastMCP(
        name=service.name,
        instructions=instructions,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=service.base, resource_server_url=service.resource, required_scopes=[service.scope],
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[service.scope],
                                                                  default_scopes=[service.scope]),
            revocation_options=RevocationOptions(enabled=False)),  # own /revoke below
        streamable_http_path=prefix + "/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[urlparse(service.public).netloc, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[service.public, "https://chatgpt.com", "https://chat.openai.com", "https://claude.ai",
                             "http://127.0.0.1:*", "http://localhost:*"]),
    )

    def client_address(request: Request) -> str:
        host = request.client.host if request.client else "?"
        return request.headers.get("x-real-ip", host) if host == "127.0.0.1" else host

    def local_path(value: str) -> str:
        return value if value.startswith(prefix + "/") and not value.startswith("//") else prefix + "/"

    @mcp.custom_route(prefix + "/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        params = dict(request.query_params) if request.method == "GET" else dict(await request.form())
        txn, next_path = params.get("txn", ""), local_path(params.get("next", prefix + "/"))
        pending = provider.pending.get(txn) if txn else None
        if txn and pending is None:
            return HTMLResponse(page(service.name, f"<h1>{service.name}</h1><p>This sign-in link expired. "
                                                   "Start connecting again from the app.</p>"),
                                status_code=400, headers=PAGE_HEADERS)
        if pending:
            redirect_host = urlparse(str(pending["params"].redirect_uri)).netloc
            prompt = (f"<b>{html.escape(pending['client_name'])}</b> ({html.escape(redirect_host)}) wants to read "
                      f"{html.escape(service.what)}. Enter your passphrase to allow it.")
            hidden = {"txn": txn}
        else:
            prompt, hidden = f"Enter your passphrase to open {html.escape(service.what)}.", {"next": next_path}
        if request.method == "GET":
            return HTMLResponse(login_page(service, prompt, hidden), headers=PAGE_HEADERS)
        address = client_address(request)
        if ctx.throttle.locked(address):
            log(f"login: locked for {address}")
            return HTMLResponse(login_page(service, prompt, hidden, "Too many wrong passphrases. Try again in 15 minutes."),
                                status_code=429, headers=PAGE_HEADERS)
        if not await asyncio.to_thread(check_passphrase, service.passphrase_file, params.get("passphrase", "")):
            ctx.throttle.fail(address)
            log(f"login: wrong passphrase from {address}")
            return HTMLResponse(login_page(service, prompt, hidden, "Wrong passphrase."), status_code=401, headers=PAGE_HEADERS)
        ctx.throttle.succeed(address)
        if pending:
            log(f"login: approved {pending['client_name']!r} from {address}")
            return RedirectResponse(provider.approve(txn), status_code=302, headers={"Cache-Control": "no-store"})
        log(f"login: browser session from {address}")
        response = RedirectResponse(next_path, status_code=303)
        expires = str(int(time.time()) + SESSION_TTL)
        response.set_cookie(ctx.cookie, f"{expires}.{ctx.keys.sign('session', expires)}", max_age=SESSION_TTL,
                            path=prefix, secure=service.public.startswith("https"), httponly=True, samesite="lax")
        return response

    @mcp.custom_route(prefix + "/revoke", methods=["POST"])
    async def revoke(request: Request) -> Response:
        """RFC 7009. Own handler: the SDK's insists on a client_secret even for public clients."""
        form = await request.form()
        client = await provider.get_client(str(form.get("client_id", "")))
        if client is None or (client.client_secret and not hmac.compare_digest(
                str(form.get("client_secret", "")).encode(), client.client_secret.encode())):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        token = str(form.get("token", ""))
        record = await provider.load_access_token(token) or await provider.load_refresh_token(client, token)
        if record is not None and record.client_id == client.client_id:
            await provider.revoke_token(record)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/_connector/as-metadata", methods=["GET"])
    async def as_metadata(request: Request) -> Response:
        base = service.base
        metadata = {
            "issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register", "revocation_endpoint": base + "/revoke",
            "scopes_supported": [service.scope], "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
            "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
            "code_challenge_methods_supported": ["S256"],
        }
        return JSONResponse(metadata, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=300"})

    return ctx


# ---- remote control of open players ------------------------------------------------------------
# A player (the MCP Apps view in ChatGPT/Claude, or the browser page) long-polls its commands from
# <prefix>/ctl/<player>/<exp>/<sig> and posts its state there. player_control sends a command to the
# open player the user started or touched last; player_status reads its state. Players live in
# memory: after a restart an open player re-registers itself with its signed link.
PLAYER_ALIVE = 70.0          # a player polls at least every 25 s; silent longer = closed
PLAYER_KEEP = 6 * 3600
CONTROL_TTL = 24 * 3600
# None of these can start sound: the user always presses play themselves (2026-10-24).
PLAYER_ACTIONS = ("pause", "stop", "seek", "forward", "back", "next_line", "previous_line",
                  "repeat_on", "repeat_off", "speed")


@dataclass
class Player:
    id: str
    item_id: str
    title: str
    seen: float
    active: float            # opened, or last played/paused/seeked by the user: commands go to the latest
    status: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)  # [seq, action, value, extra]
    seq: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    push: Any = None         # a connected device (the MyMedia phone app): sends a command over its WebSocket


class Players:
    def __init__(self):
        self.all: dict[str, Player] = {}

    def open(self, item_id: str, title: str, player_id: str | None = None) -> Player:
        now = time.time()
        for old in [k for k, p in self.all.items() if now - p.seen > PLAYER_KEEP]:
            del self.all[old]
        player = Player(player_id or secrets.token_urlsafe(12), item_id, title[:200], seen=now, active=now)
        self.all[player.id] = player
        return player

    def target(self) -> Player | None:
        now = time.time()
        alive = [p for p in self.all.values() if now - p.seen < PLAYER_ALIVE or p.push is not None]
        return max(alive, key=lambda p: p.active, default=None)

    def send(self, player: Player, action: str, value: float, extra: dict | None = None) -> int:
        player.seq += 1
        player.commands = [*player.commands[-19:], [player.seq, action, value, extra or {}]]
        player.changed.set()
        if player.push is not None:
            player.push({"type": "command", "seq": player.seq, "action": action, "value": value, **(extra or {})})
        return player.seq

    def device(self) -> Player | None:
        """The connected phone app (the most recently connected one)."""
        connected = [p for p in self.all.values() if p.push is not None]
        return max(connected, key=lambda p: p.seen, default=None)

    async def commands_after(self, player: Player, after: int, wait: float) -> list:
        player.seen = time.time()
        if not any(c[0] > after for c in player.commands):
            player.changed.clear()
            try:
                await asyncio.wait_for(player.changed.wait(), wait)
            except asyncio.TimeoutError:
                pass
        player.seen = time.time()
        return [c for c in player.commands if c[0] > after]

    def report(self, player: Player, status: dict):
        now = time.time()
        player.seen = 0.0 if status.get("closed") is True else now  # closed: no longer a target
        keep = {}
        for key, kind in (("playing", bool), ("blocked", bool), ("position_s", (int, float)), ("duration_s", (int, float)),
                          ("rate", (int, float)), ("line", int), ("applied", int), ("repeat", bool)):
            if isinstance(status.get(key), kind):
                keep[key] = status[key]
        if isinstance(status.get("line_text"), str):
            keep["line_text"] = status["line_text"][:500]
        before = (bool(player.status), player.status.get("playing"), player.status.get("blocked"))
        player.status.update(keep)
        if status.get("user"):  # the user played, paused or moved it: this is the player they are using
            player.active = now
        after = (True, player.status.get("playing"), player.status.get("blocked"))
        if status.get("closed") is True:
            log(f"player {player.id[:6]} {player.title!r}: closed")
        elif after != before:
            state = "playing" if after[1] else "blocked (needs a tap)" if after[2] else "paused"
            log(f"player {player.id[:6]} {player.title!r}: {state}" + ("" if before[0] else " (connected)")
                + (f" at {clock_s(player.status.get('position_s'))}" if player.status.get("position_s") else ""))


def clock_s(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60:02d}:{seconds % 60:02d}"


def describe_player(player: Player) -> str:
    s = player.status
    if not s:
        return f"{player.title}: opened, not started yet."
    state = "playing" if s.get("playing") else "paused"
    text = f"{player.title}: {state} at {clock_s(s.get('position_s'))}"
    if s.get("duration_s"):
        text += f" of {clock_s(s.get('duration_s'))}"
    if s.get("rate") and s.get("rate") != 1:
        text += f", speed {s['rate']}×"
    if s.get("repeat"):
        text += ", repeating the line"
    if s.get("line_text"):
        text += f". Current line: \u201c{s['line_text']}\u201d"
    return text + "."


class Devices:
    """Paired phone apps (state/devices.json): device id -> name and the SHA-256 of its key. Pairing takes the
    passphrase once; the app then keeps only its own key, which revoke-all removes with everything else."""

    def __init__(self, state: Path):
        self.path = state / "devices.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def pair(self, name: str) -> tuple[str, str]:
        device_id, key = secrets.token_hex(6), secrets.token_urlsafe(32)
        self.data[device_id] = {"name": name[:80] or "phone", "key_sha256": digest(key), "paired_at": int(time.time())}
        write_atomic(self.path, json.dumps(self.data, indent=1).encode())
        return device_id, key

    def check(self, key: str) -> str | None:
        wanted = digest(key)
        return next((d for d, v in self.data.items() if hmac.compare_digest(v["key_sha256"], wanted)), None)


def client_ip(request) -> str:
    """The caller's address: Caddy's X-Real-IP when the request came through it, else the peer."""
    host = request.client.host if request.client else "?"
    return request.headers.get("x-real-ip", host) if host == "127.0.0.1" else host


async def wait_applied(player: Player, seq: int, seconds: float = 5.0) -> bool:
    """Whether the player confirmed command seq within seconds (it reports applied=<seq>)."""
    for _ in range(int(seconds / 0.2)):
        if player.status.get("applied", 0) >= seq:
            return True
        await asyncio.sleep(0.2)
    return player.status.get("applied", 0) >= seq


def add_player_control(ctx: "Context"):
    """Routes and the player_control / player_status tools; returns control_url(player) for players."""
    from mcp.types import CallToolResult, TextContent, ToolAnnotations
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from typing import Literal

    service, keys, mcp = ctx.service, ctx.keys, ctx.mcp
    players = ctx.players = Players()
    cors = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}

    def control_url(player: Player) -> str:
        expires = str(int(time.time()) + CONTROL_TTL)
        return f"{service.base}/ctl/{player.id}/{expires}/{keys.sign('c', player.id, expires)}"

    @mcp.custom_route(service.prefix + "/ctl/{player_id}/{expires}/{signature}", methods=["GET", "POST"])
    async def control(request: Request) -> Response:
        player_id, expires, signature = (request.path_params[k] for k in ("player_id", "expires", "signature"))
        if not (re.fullmatch(r"[A-Za-z0-9_-]{8,32}", player_id) and expires.isdigit() and int(expires) > time.time()
                and keys.check(signature, "c", player_id, expires)):
            return JSONResponse({"error": "link expired"}, status_code=403, headers=cors)
        player = players.all.get(player_id)
        if player is None:  # server restarted: the open player registers again under its signed id
            player = players.open(request.query_params.get("item", "")[:64], request.query_params.get("title", "player"), player_id)
        if request.method == "POST":
            try:
                status = json.loads(await request.body() or b"{}")
            except ValueError:
                return JSONResponse({"error": "bad status"}, status_code=400, headers=cors)
            if isinstance(status, dict):
                players.report(player, status)
            return JSONResponse({"ok": True}, headers=cors)
        try:
            after = int(request.query_params.get("after", "0"))
            wait = max(0.0, min(float(request.query_params.get("wait", "25")), 25.0))
        except ValueError:
            after, wait = 0, 25.0
        pending = await players.commands_after(player, after, wait)
        return JSONResponse({"commands": [{"seq": c[0], "action": c[1], "value": c[2], **c[3]} for c in pending]}, headers=cors)

    @mcp.tool(name="player_control", title="Control the player",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=False))
    async def player_control(action: Literal["pause", "stop", "seek", "forward", "back", "next_line",
                                             "previous_line", "repeat_on", "repeat_off", "speed"],
                             value: float = 0) -> CallToolResult:
        """Control the player that is open on the user's screen (the one opened or used last): pause, stop (pause
        and go to the start), seek (to second value), forward/back (by value seconds, default 10),
        next_line/previous_line, repeat_on/repeat_off (loop the current line), speed (value, e.g. 0.75). It cannot
        start playback: only the user presses play. Returns the player's state after the command."""
        if action not in PLAYER_ACTIONS:
            raise LookupError(f"{action} is not possible from here; only the user can start playback.")
        player = players.target()
        if player is None:
            raise LookupError("No player is open on the user's screen right now; open one with play first.")
        seq = players.send(player, action, float(value))
        confirmed = await wait_applied(player, seq)
        text = describe_player(player) if confirmed else (
            f"Sent {action} to {player.title}, but the player has not confirmed yet (the screen may be off); "
            "it will apply the command when it is back.")
        return CallToolResult(content=[TextContent(type="text", text=text)])

    @mcp.tool(name="player_status", title="What is playing",
              annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True))
    async def player_status() -> CallToolResult:
        """What the open player is doing: item, playing or paused, position, speed and the line being spoken
        (when the item has timed text). Use it for "what did they just say?" or "where am I?"."""
        player = players.target()
        if player is None:
            return CallToolResult(content=[TextContent(type="text", text="No player is open on the user's screen right now.")])
        return CallToolResult(content=[TextContent(type="text", text=describe_player(player))])

    return control_url


def app(ctx: Context):
    """The ASGI app: the SDK's OAuth routes answer under the prefix, nothing else reaches it via Caddy."""
    from starlette.responses import PlainTextResponse
    prefix = ctx.service.prefix
    inner = ctx.mcp.streamable_http_app()
    rewrites = {prefix + p: p for p in ("/authorize", "/token", "/register")}
    for path in ("/.well-known/oauth-authorization-server" + prefix, prefix + "/.well-known/oauth-authorization-server",
                 "/.well-known/openid-configuration" + prefix, prefix + "/.well-known/openid-configuration"):
        rewrites[path] = "/_connector/as-metadata"

    def public(path: str) -> bool:
        return path == prefix or path.startswith((prefix + "/", "/.well-known/oauth-protected-resource" + prefix))

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path in rewrites:
                scope = {**scope, "path": rewrites[path], "raw_path": rewrites[path].encode()}
            elif any(name == b"x-real-ip" for name, _ in scope.get("headers", [])) and not public(path):
                # through Caddy (it sets X-Real-IP): only the public paths, the SDK's own ones are rewritten above
                return await PlainTextResponse("not found", status_code=404)(scope, receive, send)
        await inner(scope, receive, send)

    asgi.ctx, asgi.inner = ctx, inner
    return asgi


def main(service: Service, make_app, argv: list[str]):
    """serve (default) | set-passphrase [--stdin] | revoke-all | status"""
    command = argv[1] if len(argv) > 1 else "serve"
    service.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if command == "set-passphrase":
        passphrase = sys.stdin.readline().strip() if "--stdin" in argv else new_passphrase()
        if len(normalize_passphrase(passphrase)) < 12:
            sys.exit("passphrase too short (at least 12 letters or digits)")
        set_passphrase(service.passphrase_file, passphrase)
        print(passphrase if "--stdin" not in argv else "passphrase set")
    elif command == "revoke-all":
        for name in ("oauth.json", "secret.key", "devices.json"):
            (service.state / name).unlink(missing_ok=True)
        print(f"revoked: every {service.name} connection and browser session; restart the service")
    elif command == "status":
        try:
            data = json.loads((service.state / "oauth.json").read_text())
        except (OSError, ValueError):
            data = {}
        now = time.time()
        print(json.dumps({"passphrase_set": service.passphrase_file.exists(),
                          "clients": [c.get("client_name") for c in data.get("clients", {}).values()],
                          "live_refresh_tokens": sum(1 for v in data.get("refresh", {}).values() if v["expires_at"] > now)}))
    elif command == "serve":
        import uvicorn
        if not service.passphrase_file.exists():
            log(f"warning: no passphrase in {service.passphrase_file}; run `set-passphrase` (nobody can sign in until then)")
        uvicorn.run(make_app(), host="127.0.0.1", port=service.port, proxy_headers=False, log_level="warning")
    else:
        sys.exit(main.__doc__)
