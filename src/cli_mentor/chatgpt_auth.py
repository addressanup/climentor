"""Sign in with ChatGPT: teach with the learner's ChatGPT plan instead of an API key.

This is OpenAI's documented flow for open-source apps that run locally
(https://developers.openai.com/siwc/token-sharing-open-source). It uses the
same plan usage as Codex, but through the public Responses API, not Codex's
private sign-in.

1. The first sign-in registers CLI Mentor as the learner's own app
   (``client_id=dynamic_agent_client``). OpenAI returns an issued client id,
   which is kept and reused for later sign-ins.
2. The browser comes back to a one-shot page on 127.0.0.1. We check the
   ``state``, swap the one-time code for tokens (PKCE, no secret), and verify
   the ID token's signature against OpenAI's published keys.
3. Tokens live in the macOS Keychain, never in a file. Access tokens last an
   hour and are renewed with the refresh token. Renewals are serialised with a
   file lock so two sessions cannot race a rotating refresh token.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
import webbrowser
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import jwt

from .apikeys import delete_api_key, get_api_key, set_api_key
from .logs import register_secret
from .paths import AppPaths, ensure_private_dir, write_private_file

log = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
CALLBACK_PATH = "/auth/callback"
DEFAULT_PORT = 1455
APP_NAME = "CLI Mentor"
USAGE_URL = "https://chatgpt.com/settings/usage"
DYNAMIC_CLIENT = "dynamic_agent_client"
KEYCHAIN_ACCOUNT = "chatgpt"  # one Keychain item (service 'cli-mentor') holds the whole sign-in record
REFRESH_MARGIN = 300.0  # renew access tokens this many seconds before they expire
PLAN_OFF = (
    "You signed in, but did not allow CLI Mentor to use your ChatGPT plan. "
    "Run `mentor login` again and allow it, or use an API key with `mentor init`."
)
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
# Refresh errors that mean the saved session is gone for good (sign in again).
_DEAD_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


class SignInError(Exception):
    """A sign-in problem with a plain message. Never contains a token."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class Account:
    """One ChatGPT sign-in. The tokens are secrets, so ``repr`` leaves them out."""

    client_id: str
    subject: str = ""
    email: str = ""
    name: str = ""
    scopes: list[str] = field(default_factory=list)
    id_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_at: float = 0.0
    saved_at: float = 0.0

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    @property
    def plan_enabled(self) -> bool:
        return PLAN_SCOPE in self.scopes

    @property
    def label(self) -> str:
        return self.email or self.name or "your ChatGPT account"

    def signed_out(self) -> Account:
        """The registration without tokens, kept so the next sign-in reuses the same app."""
        return Account(self.client_id, self.subject, self.email, self.name)


class CredentialStore(Protocol):
    def load(self) -> Account | None: ...

    def save(self, account: Account) -> None: ...

    def delete(self) -> None: ...


class KeychainStore:
    """The sign-in record as one JSON item in the macOS Keychain."""

    def load(self) -> Account | None:
        raw = get_api_key(KEYCHAIN_ACCOUNT).key
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Account(**{f.name: data[f.name] for f in fields(Account) if f.name in data})
        except (ValueError, TypeError, KeyError):
            log.warning("ignoring an unreadable ChatGPT sign-in record")
            return None

    def save(self, account: Account) -> None:
        set_api_key(KEYCHAIN_ACCOUNT, json.dumps(asdict(account)))

    def delete(self) -> None:
        delete_api_key(KEYCHAIN_ACCOUNT)


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _on_issuer(value: object) -> bool:
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    return url.scheme == "https" and url.netloc == urlsplit(ISSUER).netloc


def _remember_secrets(account: Account) -> None:
    for token in (account.access_token, account.refresh_token, account.id_token):
        register_secret(token)


def _token_fields(data: Mapping[str, Any], scopes_if_missing: list[str] | None = None) -> dict[str, Any]:
    """Check a token response and pick out what we keep."""
    scope = data.get("scope")
    if isinstance(scope, str):
        scopes = scope.split()
    elif scope is None and scopes_if_missing is not None:  # a refresh may leave scope out when it is unchanged
        scopes = list(scopes_if_missing)
    else:
        raise SignInError("invalid_token_response", "ChatGPT did not confirm what you allowed. Please sign in again.")
    access, refresh = data.get("access_token"), data.get("refresh_token")
    kind, expires = data.get("token_type"), data.get("expires_in")
    if not (
        isinstance(access, str)
        and access
        and isinstance(refresh, str)
        and refresh
        and isinstance(kind, str)
        and kind.lower() == "bearer"
        and isinstance(expires, int | float)
        and not isinstance(expires, bool)
        and expires > 0
    ):
        raise SignInError(
            "invalid_token_response", "ChatGPT returned incomplete sign-in details. Please sign in again."
        )
    now = time.time()
    return {
        "scopes": scopes,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": now + float(expires),
        "saved_at": now,
    }


# ---------------------------------------------------------------- the page the browser returns to
_PAGE = (
    '<!doctype html><html lang="en"><meta charset="utf-8"><title>CLI Mentor</title>'
    "<style>body{{font:17px system-ui;max-width:32rem;margin:18vh auto;padding:24px}}h1{{font-size:24px}}</style>"
    "<h1>{title}</h1><p>{text}</p></html>"
)
_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found"}


class _Callback:
    """A one-shot web page on 127.0.0.1 that receives the browser's return from sign-in."""

    def __init__(self, port: int, state: str, saved_client_id: str | None) -> None:
        self.port = port
        self.state = state
        self.saved = saved_client_id
        self.result: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> _Callback:
        try:
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        except OSError:  # port busy: only the port may vary, so any free one works
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if not self.result.done():
            self.result.cancel()
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.server.wait_closed(), 2)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            status, title, text = self._answer(head.decode("latin-1"))
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            status, title, text = 400, "Bad request", ""
        body = _PAGE.format(title=title, text=text).encode()
        writer.write(
            (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        with contextlib.suppress(OSError):
            await writer.drain()
        writer.close()

    def _answer(self, head: str) -> tuple[int, str, str]:
        request_line, *header_lines = head.split("\r\n")
        method, target, _version = request_line.split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in header_lines if ":" in h)}
        url = urlsplit(target)
        if (
            method != "GET"
            or url.path != CALLBACK_PATH
            or headers.get("host") != f"127.0.0.1:{self.port}"
            or self.result.done()
        ):
            return 404, "Not found", ""
        query = parse_qs(url.query, keep_blank_values=True)
        state = query.get("state", [])
        if len(state) != 1 or not hmac.compare_digest(state[0].encode(), self.state.encode()):
            # An unrelated or old request must not end the sign-in that is waiting.
            return 400, "This sign-in link is out of date", "Go back to the terminal and start the sign-in again."
        if "error" in query:
            code = query["error"][0]
            message = (
                "You cancelled the sign-in or did not allow it."
                if code == "access_denied"
                else f"ChatGPT sign-in failed ({code})."
            )
            self.result.set_exception(SignInError(code, message))
            return 200, "Sign-in stopped", "You can close this tab and go back to the terminal."
        codes, returned = query.get("code", []), query.get("client_id", [])
        client_id = returned[0] if len(returned) == 1 else self.saved
        if (
            len(codes) != 1
            or len(returned) > 1
            or not client_id
            or not _CLIENT_ID.match(client_id)
            or client_id == DYNAMIC_CLIENT
            or (self.saved is not None and returned and returned[0] != self.saved)
        ):
            self.result.set_exception(
                SignInError("registration_incomplete", "ChatGPT did not finish registering CLI Mentor. Sign in again.")
            )
            return 400, "Sign-in did not finish", "Go back to the terminal and try again."
        self.result.set_result((codes[0], client_id))
        return 200, "You're signed in", "You can close this tab and go back to the terminal."


# ---------------------------------------------------------------- sign-in, renewal, sign-out
class ChatGPTAuth:
    def __init__(
        self,
        paths: AppPaths,
        *,
        store: CredentialStore | None = None,
        http: httpx2.AsyncClient | None = None,
        open_browser: Callable[[str], object] | None = None,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.paths = paths
        self.store: CredentialStore = store or KeychainStore()
        self._http = http
        self.open_browser = open_browser or webbrowser.open
        self.port = port
        self._endpoints: dict[str, str] | None = None
        self._keys: jwt.PyJWKSet | None = None

    def account(self) -> Account | None:
        return self.store.load()

    def host_id(self) -> str:
        """This computer's stable id for OpenAI. Random and opaque: not a secret, not about the learner."""
        path = self.paths.data_dir / "chatgpt-host-id"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value.startswith("urn:uuid:"):
                return value
        ensure_private_dir(self.paths.data_dir)
        value = f"urn:uuid:{uuid.uuid4()}"
        write_private_file(path, value + "\n")
        return value

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx2.AsyncClient]:
        if self._http is not None:
            yield self._http
            return
        async with httpx2.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            yield client

    async def endpoints(self) -> dict[str, str]:
        """OpenAI's sign-in addresses, checked to belong to auth.openai.com."""
        if self._endpoints is None:
            try:
                async with self._client() as http:
                    res = await http.get(f"{ISSUER}/.well-known/openid-configuration")
                data = res.json() if res.status_code == 200 else None
            except (httpx2.HTTPError, ValueError):
                data = None
            needed = ("authorization_endpoint", "token_endpoint", "jwks_uri")
            if (
                not isinstance(data, dict)
                or data.get("issuer") != ISSUER
                or not all(_on_issuer(data.get(k)) for k in needed)
                or ("revocation_endpoint" in data and not _on_issuer(data["revocation_endpoint"]))
            ):
                raise SignInError(
                    "discovery_failed",
                    "Could not reach ChatGPT sign-in (auth.openai.com). Check your connection and try again.",
                    retryable=True,
                )
            self._endpoints = {k: str(data[k]) for k in (*needed, "revocation_endpoint") if k in data}
        return self._endpoints

    async def sign_in(self, on_url: Callable[[str], None] | None = None, *, timeout: float = 600.0) -> Account:
        """Open the browser, wait for the learner, then save the verified sign-in.

        Check ``plan_enabled`` on the result: the learner may sign in but not
        allow CLI Mentor to use their plan.
        """
        previous = self.account()
        saved_id = previous.client_id if previous else None
        endpoints = await self.endpoints()
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with _Callback(self.port, state, saved_id) as callback:
            redirect_uri = callback.redirect_uri
            params = {
                "client_id": saved_id or DYNAMIC_CLIENT,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": _challenge(verifier),
                "ext_agent_host_id": self.host_id(),
            }
            if saved_id is None:
                params["agent_name_hint"] = APP_NAME
            if previous is not None and previous.email:
                params["login_hint"] = previous.email
            if previous is not None and previous.scopes and not previous.plan_enabled:
                params["prompt"] = "consent"  # they said no before and now ask to allow it
            url = f"{endpoints['authorization_endpoint']}?{urlencode(params)}"
            if on_url is not None:
                on_url(url)
            try:
                self.open_browser(url)
            except Exception:  # the link was shown; they can open it by hand
                log.warning("could not open the browser for ChatGPT sign-in")
            try:
                code, client_id = await asyncio.wait_for(callback.result, timeout)
            except TimeoutError:
                raise SignInError("timeout", "The sign-in timed out. Try again when you are ready.") from None
        if saved_id != client_id:  # keep a new registration even if the code swap below fails
            self.store.save(Account(client_id))
        data = await self._token(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "resource": RESOURCE,
            }
        )
        id_token = data.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise SignInError("invalid_id_token", "ChatGPT did not return a verifiable identity. Sign in again.")
        claims = await self._verify(id_token, client_id, nonce)
        if previous is not None and previous.subject and claims["sub"] != previous.subject:
            raise SignInError(
                "account_mismatch",
                "That is a different ChatGPT account from the one CLI Mentor was set up with. "
                "To switch accounts, run `mentor logout --forget`, then `mentor login`.",
            )
        account = Account(
            client_id=client_id,
            subject=claims["sub"],
            email=str(claims.get("email") or ""),
            name=str(claims.get("name") or ""),
            id_token=id_token,
            **_token_fields(data),
        )
        self.store.save(account)
        _remember_secrets(account)
        return account

    async def access_token(self) -> str:
        """A valid access token. Renews it first when it is close to expiry."""
        account = self._usable(self.account())
        if account.expires_at - time.time() <= REFRESH_MARGIN:
            async with self._refresh_lock():
                account = self._usable(self.account())  # another window may have renewed it while we waited
                if account.expires_at - time.time() <= REFRESH_MARGIN:
                    account = await self._refresh(account)
        _remember_secrets(account)
        return account.access_token

    async def sign_out(self, *, forget: bool = False) -> bool:
        """End the renewable session, then remove the tokens.

        Returns True when OpenAI confirmed the session is revoked. ``forget``
        also drops the app registration, so the next sign-in starts fresh.
        """
        account = self.account()
        if account is None:
            return True
        confirmed = await self._revoke(account) if account.refresh_token else True
        if forget:
            self.store.delete()
        else:
            self.store.save(account.signed_out())
        return confirmed

    # ------------------------------------------------------------ helpers
    def _usable(self, account: Account | None) -> Account:
        if account is None or not account.signed_in:
            raise SignInError("signed_out", "You are not signed in to ChatGPT. Run `mentor login`.")
        if not account.plan_enabled:
            raise SignInError("plan_not_enabled", PLAN_OFF)
        return account

    @contextlib.asynccontextmanager
    async def _refresh_lock(self) -> AsyncIterator[None]:
        ensure_private_dir(self.paths.data_dir)
        fd = os.open(self.paths.data_dir / "chatgpt.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise SignInError(
                            "busy", "Another CLI Mentor window is renewing the sign-in. Try again.", retryable=True
                        ) from None
                    await asyncio.sleep(0.1)
            yield
        finally:
            os.close(fd)  # closing the file releases the lock

    async def _refresh(self, account: Account) -> Account:
        try:
            data = await self._token(
                {
                    "grant_type": "refresh_token",
                    "client_id": account.client_id,
                    "refresh_token": account.refresh_token,
                    "resource": RESOURCE,
                }
            )
        except SignInError as exc:
            if exc.code in _DEAD_REFRESH:
                self.store.save(account.signed_out())
                raise SignInError("expired", "Your ChatGPT sign-in has expired. Run `mentor login` again.") from None
            raise
        renewed = replace(account, **_token_fields(data, account.scopes))
        id_token = data.get("id_token")
        if isinstance(id_token, str) and id_token:
            renewed.id_token = id_token
        self.store.save(renewed)  # save at once: the old refresh token no longer works
        if not (isinstance(id_token, str) and id_token):
            return renewed
        try:
            claims = await self._verify(id_token, account.client_id, None)
        except SignInError as exc:
            if exc.retryable:  # OpenAI's keys were unreachable; the renewed tokens still came from OpenAI
                return renewed
            self.store.save(account.signed_out())
            raise
        if claims["sub"] != account.subject:
            self.store.save(account.signed_out())
            raise SignInError("account_mismatch", "The renewed ChatGPT identity did not match. Run `mentor login`.")
        return renewed

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        endpoints = await self.endpoints()
        try:
            async with self._client() as http:
                res = await http.post(endpoints["token_endpoint"], data=form, headers={"accept": "application/json"})
        except httpx2.HTTPError:
            raise SignInError(
                "network_error", "Could not reach ChatGPT sign-in. Check your connection and try again.", retryable=True
            ) from None
        try:
            data = res.json()
        except ValueError:
            data = None
        if res.status_code != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            code = str(
                (error.get("code") if isinstance(error, dict) else error) or ""
            )  # {"error": "x"} or {"error": {"code": "x"}}
            raise SignInError(
                code or f"http_{res.status_code}",
                f"ChatGPT sign-in was refused ({code or f'HTTP {res.status_code}'}).",
                retryable=res.status_code >= 500,
            )
        return data

    async def _verify(self, id_token: str, client_id: str, nonce: str | None) -> dict[str, Any]:
        """Check the ID token's signature, issuer, audience, expiry and nonce."""
        invalid = SignInError("invalid_id_token", "Your ChatGPT identity could not be verified. Sign in again.")
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError:
            raise invalid from None
        key = await self._signing_key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=ISSUER,
                leeway=5,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
        except jwt.PyJWTError:
            raise invalid from None
        aud, azp = claims.get("aud"), claims.get("azp")
        if (
            not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or (nonce is not None and claims.get("nonce") != nonce)
            or (azp is not None and azp != client_id)
            or (isinstance(aud, list) and len(aud) > 1 and azp != client_id)
        ):
            raise invalid
        return claims

    async def _signing_key(self, kid: object) -> Any:
        for reload in (False, True):  # OpenAI rotates keys: reload once when the id is unknown
            if self._keys is None or reload:
                endpoints = await self.endpoints()
                try:
                    async with self._client() as http:
                        res = await http.get(endpoints["jwks_uri"])
                    self._keys = jwt.PyJWKSet.from_dict(res.json()) if res.status_code == 200 else None
                except (httpx2.HTTPError, ValueError, jwt.PyJWTError):
                    self._keys = None
                if self._keys is None:
                    raise SignInError(
                        "identity_verification_unavailable",
                        "Could not check your ChatGPT identity right now. Try again shortly.",
                        retryable=True,
                    )
            for key in self._keys.keys:
                if key.key_id == kid:
                    return key.key
        raise SignInError("invalid_id_token", "Your ChatGPT identity could not be verified. Sign in again.")

    async def _revoke(self, account: Account) -> bool:
        try:
            endpoint = (await self.endpoints()).get("revocation_endpoint")
        except SignInError:
            return False
        if not endpoint:
            return False
        form = {"token": account.refresh_token, "token_type_hint": "refresh_token", "client_id": account.client_id}
        for attempt in range(2):
            try:
                async with self._client() as http:
                    res = await http.post(endpoint, data=form)
                if res.status_code == 200:
                    return True
                if res.status_code < 500:
                    return False
            except httpx2.HTTPError:
                pass
            if attempt == 0:
                await asyncio.sleep(0.3)
        return False
