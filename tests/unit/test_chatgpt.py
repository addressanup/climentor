"""Sign in with ChatGPT and the ChatGPT-plan provider, with no network.

OpenAI's sign-in server is faked with ``httpx2.MockTransport`` and an RSA key
made here. The browser is faked by a real HTTP request to the local return
page on 127.0.0.1. Nothing reaches OpenAI or the real Keychain.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cli_mentor.apikeys import scrub_keys
from cli_mentor.chatgpt_auth import (
    ISSUER,
    RESOURCE,
    USAGE_URL,
    Account,
    ChatGPTAuth,
    KeychainStore,
    SignInError,
    _challenge,
)
from cli_mentor.config import Config, ProviderSettings, save_config
from cli_mentor.doctor import run_checks
from cli_mentor.paths import AppPaths
from cli_mentor.providers import build_provider
from cli_mentor.providers.base import (
    ChatMessage,
    Done,
    ProviderError,
    ProviderRequest,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)
from cli_mentor.providers.chatgpt_provider import ChatGPTProvider
from cli_mentor.tutor.ui_protocol import StatusInfo

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}
CLIENT = "oaiapp_test123"
ALL_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
NO_PLAN = "openid profile email offline_access"


def make_id_token(*, aud: str, nonce: str | None, sub: str = "user-1") -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"iss": ISSUER, "aud": aud, "sub": sub, "iat": now, "exp": now + 3600}
    claims |= {"email": "learner@example.com", "name": "Learner"}
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


class FakeOpenAI:
    """auth.openai.com: discovery, signing keys, token and revoke endpoints."""

    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []
        self.nonce: str | None = None  # read from the sign-in link by the fake browser
        self.wrong_nonce = False
        self.scope = ALL_SCOPES
        self.sub = "user-1"
        self.refresh_error: str | None = None
        self.revoke_status = 200
        self.issued = 0

    def handler(self, req: httpx2.Request) -> httpx2.Response:
        path = req.url.path
        if path == "/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/api/accounts/authorize",
                    "token_endpoint": f"{ISSUER}/api/accounts/oauth/token",
                    "revocation_endpoint": f"{ISSUER}/api/accounts/oauth/revoke",
                    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                },
            )
        if path == "/.well-known/jwks.json":
            return httpx2.Response(200, json={"keys": [JWK]})
        form = dict(parse_qsl(req.content.decode()))
        self.forms.append(form)
        if path == "/api/accounts/oauth/revoke":
            return httpx2.Response(self.revoke_status)
        if path == "/api/accounts/oauth/token":
            if form["grant_type"] == "refresh_token" and self.refresh_error:
                return httpx2.Response(400, json={"error": self.refresh_error})
            self.issued += 1
            first = form["grant_type"] == "authorization_code"
            nonce = ("other" if self.wrong_nonce else self.nonce) if first else None
            return httpx2.Response(
                200,
                json={
                    "access_token": f"access-{self.issued}",
                    "refresh_token": f"refresh-{self.issued}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": self.scope,
                    "id_token": make_id_token(aud=form["client_id"], nonce=nonce, sub=self.sub),
                },
            )
        return httpx2.Response(404)


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


class FakeBrowser:
    """Opens the sign-in link by going straight to the local return page, as OpenAI's redirect would."""

    def __init__(self, server: FakeOpenAI, *, client_id: str | None = CLIENT, **extra: str) -> None:
        self.server, self.client_id, self.extra = server, client_id, extra
        self.urls: list[str] = []
        self.stray_first = False
        self.tasks: list[asyncio.Task[None]] = []

    def __call__(self, url: str) -> None:
        self.urls.append(url)
        q = query(url)
        self.server.nonce = q["nonce"]
        params = {"code": "code-1", "state": q["state"], **self.extra}
        if self.client_id:
            params["client_id"] = self.client_id

        async def visit() -> None:
            async with httpx2.AsyncClient() as http:  # a real request to 127.0.0.1
                if self.stray_first:
                    stray = await http.get(q["redirect_uri"], params={"state": "old", "code": "x"})
                    assert stray.status_code == 400
                await http.get(q["redirect_uri"], params=params)

        self.tasks.append(asyncio.get_running_loop().create_task(visit()))


def make_auth(server: FakeOpenAI, browser: Callable[[str], object] | None = None) -> ChatGPTAuth:
    return ChatGPTAuth(
        AppPaths.from_env(),
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(server.handler)),
        open_browser=browser or (lambda _url: None),
        port=0,
    )


def signed_in(expires_in: float = 3600, **kw: Any) -> Account:
    base: dict[str, Any] = dict(
        client_id=CLIENT,
        subject="user-1",
        email="learner@example.com",
        scopes=ALL_SCOPES.split(),
        id_token="id-0",
        access_token="access-0",
        refresh_token="refresh-0",
        expires_at=time.time() + expires_in,
    )
    return Account(**(base | kw))


# ---------------------------------------------------------------- sign-in
async def test_first_sign_in_registers_then_reuses_the_registration(
    mentor_env: dict[str, str], memory_keyring: Any
) -> None:
    server = FakeOpenAI()
    browser = FakeBrowser(server)
    auth = make_auth(server, browser)
    shown: list[str] = []
    account = await auth.sign_in(shown.append)

    q = query(browser.urls[0])
    assert browser.urls[0].startswith(f"{ISSUER}/api/accounts/authorize?") and shown == browser.urls
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "CLI Mentor"
    assert q["ext_agent_host_id"].startswith("urn:uuid:") and q["scope"] == ALL_SCOPES and q["resource"] == RESOURCE
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith("/auth/callback")
    swap = server.forms[-1]
    assert swap["grant_type"] == "authorization_code" and swap["client_id"] == CLIENT and swap["code"] == "code-1"
    assert swap["redirect_uri"] == q["redirect_uri"] and swap["resource"] == RESOURCE
    assert _challenge(swap["code_verifier"]) == q["code_challenge"]

    assert (account.client_id, account.subject, account.email) == (CLIENT, "user-1", "learner@example.com")
    assert account.signed_in and account.plan_enabled
    assert "access-1" not in repr(account) and "refresh-1" not in repr(account)
    saved = json.loads(memory_keyring.get_password("cli-mentor", "chatgpt"))
    assert saved["refresh_token"] == "refresh-1" and saved["client_id"] == CLIENT

    again = FakeBrowser(server, client_id=None)  # a returning sign-in may leave client_id out
    auth.open_browser = again
    second = await auth.sign_in()
    q2 = query(again.urls[0])
    assert q2["client_id"] == CLIENT and "agent_name_hint" not in q2 and q2["login_hint"] == "learner@example.com"
    assert q2["ext_agent_host_id"] == q["ext_agent_host_id"]  # the same computer keeps its id
    assert "prompt" not in q2 and second.access_token == "access-2"


async def test_a_stray_request_does_not_end_the_sign_in(mentor_env: dict[str, str]) -> None:
    server = FakeOpenAI()
    browser = FakeBrowser(server)
    browser.stray_first = True
    account = await make_auth(server, browser).sign_in()
    assert account.signed_in
    await asyncio.gather(*browser.tasks)


async def test_declined_sign_in_saves_nothing(mentor_env: dict[str, str]) -> None:
    server = FakeOpenAI()
    auth = make_auth(server, FakeBrowser(server, error="access_denied"))
    with pytest.raises(SignInError) as info:
        await auth.sign_in()
    assert info.value.code == "access_denied" and auth.account() is None and server.forms == []


async def test_identity_with_the_wrong_nonce_is_rejected(mentor_env: dict[str, str]) -> None:
    server = FakeOpenAI()
    server.wrong_nonce = True
    auth = make_auth(server, FakeBrowser(server))
    with pytest.raises(SignInError) as info:
        await auth.sign_in()
    assert info.value.code == "invalid_id_token"
    account = auth.account()
    assert account is not None and account.client_id == CLIENT and not account.signed_in  # registration kept


async def test_sign_in_without_plan_permission(mentor_env: dict[str, str]) -> None:
    server = FakeOpenAI()
    server.scope = NO_PLAN
    auth = make_auth(server, FakeBrowser(server))
    account = await auth.sign_in()
    assert account.signed_in and not account.plan_enabled
    with pytest.raises(SignInError) as info:
        await auth.access_token()
    assert info.value.code == "plan_not_enabled"
    again = FakeBrowser(server, client_id=None)
    auth.open_browser = again
    server.scope = ALL_SCOPES
    assert (await auth.sign_in()).plan_enabled
    assert query(again.urls[0])["prompt"] == "consent"  # asking again after saying no


async def test_a_different_account_does_not_replace_the_saved_one(mentor_env: dict[str, str]) -> None:
    KeychainStore().save(signed_in())
    server = FakeOpenAI()
    server.sub = "someone-else"
    auth = make_auth(server, FakeBrowser(server))
    with pytest.raises(SignInError) as info:
        await auth.sign_in()
    assert info.value.code == "account_mismatch" and "logout --forget" in info.value.message
    account = auth.account()
    assert account is not None and account.access_token == "access-0"


# ---------------------------------------------------------------- renewal and sign-out
async def test_access_token_renews_near_expiry(mentor_env: dict[str, str]) -> None:
    KeychainStore().save(signed_in(expires_in=10))
    server = FakeOpenAI()
    auth = make_auth(server)
    assert await auth.access_token() == "access-1"
    assert server.forms[-1] == {
        "grant_type": "refresh_token",
        "client_id": CLIENT,
        "refresh_token": "refresh-0",
        "resource": RESOURCE,
    }
    account = auth.account()
    assert account is not None and account.refresh_token == "refresh-1"  # the rotated token is kept
    count = len(server.forms)
    assert await auth.access_token() == "access-1" and len(server.forms) == count  # still fresh: no request


async def test_a_dead_refresh_token_signs_out_but_keeps_the_registration(mentor_env: dict[str, str]) -> None:
    KeychainStore().save(signed_in(expires_in=0))
    server = FakeOpenAI()
    server.refresh_error = "invalid_grant"
    auth = make_auth(server)
    with pytest.raises(SignInError) as info:
        await auth.access_token()
    assert info.value.code == "expired" and "mentor login" in info.value.message
    account = auth.account()
    assert account is not None and account.client_id == CLIENT and not account.signed_in


@pytest.mark.parametrize(
    "body", [{"error": "refresh_token_reused"}, {"error": {"code": "refresh_token_reused", "message": "x"}}]
)
async def test_refresh_error_codes_are_read_in_either_shape(mentor_env: dict[str, str], body: dict[str, Any]) -> None:
    KeychainStore().save(signed_in(expires_in=0))
    server = FakeOpenAI()

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json=body) if req.url.path.endswith("/token") else server.handler(req)

    auth = ChatGPTAuth(AppPaths.from_env(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(SignInError) as info:
        await auth.access_token()
    assert info.value.code == "expired"


async def test_sign_out_revokes_then_clears(mentor_env: dict[str, str]) -> None:
    KeychainStore().save(signed_in())
    server = FakeOpenAI()
    auth = make_auth(server)
    assert await auth.sign_out() is True
    assert server.forms[-1] == {"token": "refresh-0", "token_type_hint": "refresh_token", "client_id": CLIENT}
    account = auth.account()
    assert account is not None and account.client_id == CLIENT and not account.signed_in

    KeychainStore().save(signed_in())
    server.revoke_status = 400
    assert await auth.sign_out(forget=True) is False  # not confirmed, but removed here anyway
    assert auth.account() is None


# ---------------------------------------------------------------- the provider
class FakeAuth:
    def __init__(self, error: SignInError | None = None) -> None:
        self.error = error

    async def access_token(self) -> str:
        if self.error:
            raise self.error
        return "access-xyz"


def sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def streaming(body: bytes, seen: list[httpx2.Request] | None = None) -> httpx2.AsyncClient:
    def handler(req: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(req)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return client(handler)


REQUEST = ProviderRequest(
    model="",
    system="You are a tutor.",
    messages=[ChatMessage("user", "hi")],
    max_output_tokens=200,
    tools=[
        ToolSpec("propose_memory", "Propose a memory", {"type": "object", "properties": {"text": {"type": "string"}}})
    ],
)
USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
GOOD_STREAM = sse(
    [
        {"type": "response.created", "response": {"id": "r1", "status": "in_progress", "output": []}},
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": "Hel"},
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": "lo"},
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "namespace": "mentor",
                "name": "propose_memory",
                "arguments": '{"text": "x"}',
                "status": "completed",
            },
        },
        {"type": "response.completed", "response": {"id": "r1", "status": "completed", "output": [], "usage": USAGE}},
    ]
)


async def test_chatgpt_stream_follows_the_plan_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "org-from-an-api-account")
    seen: list[httpx2.Request] = []
    p = ChatGPTProvider(FakeAuth(), ProviderSettings(model="gpt-test"), http_client=streaming(GOOD_STREAM, seen))
    events = [e async for e in p.stream(REQUEST)]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
    assert ToolCall("propose_memory", {"text": "x"}, "call_1") in events
    assert Usage(10, 5, False) in events and events[-1] == Done("completed")
    req = seen[0]
    assert str(req.url) == f"{RESOURCE}/responses" and req.headers["authorization"] == "Bearer access-xyz"
    assert "openai-organization" not in req.headers
    body = json.loads(req.content)
    assert body["store"] is False and body["stream"] is True and body["model"] == "gpt-test"
    assert body["instructions"] == "You are a tutor." and body["input"] == [{"role": "user", "content": "hi"}]
    assert not {"max_output_tokens", "temperature", "top_p", "previous_response_id", "metadata", "user"} & body.keys()
    tools = body["tools"]
    assert len(tools) == 1 and tools[0]["type"] == "namespace" and tools[0]["tools"][0]["name"] == "propose_memory"


async def test_usage_limit_in_the_stream_points_to_usage_settings() -> None:
    failed = sse(
        [
            {
                "type": "response.failed",
                "response": {
                    "id": "r1",
                    "status": "failed",
                    "output": [],
                    "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"},
                },
            }
        ]
    )
    p = ChatGPTProvider(FakeAuth(), ProviderSettings(model="m"), http_client=streaming(failed))
    with pytest.raises(ProviderError) as info:
        [e async for e in p.stream(REQUEST)]
    assert info.value.kind == "rate_limit" and USAGE_URL in info.value.user_message()


@pytest.mark.parametrize(
    "status,body,kind,words",
    [
        (
            429,
            {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "x"}},
            "rate_limit",
            USAGE_URL,
        ),
        (401, {"detail": "not accepted"}, "auth", "mentor login"),
        (
            400,
            {"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools", "message": "x"}},
            "bad_request",
            "(tools)",
        ),
        (403, {"error": {"code": "subscription_sharing_user_not_eligible", "message": "x"}}, "auth", "API key"),
        (503, {"detail": "busy"}, "unavailable", ""),
    ],
)
async def test_chatgpt_http_errors(status: int, body: dict[str, Any], kind: str, words: str) -> None:
    p = ChatGPTProvider(
        FakeAuth(), ProviderSettings(model="m"), http_client=client(lambda _r: httpx2.Response(status, json=body))
    )
    with pytest.raises(ProviderError) as info:
        [e async for e in p.stream(REQUEST)]
    assert info.value.kind == kind and words in info.value.user_message()


async def test_a_cut_off_stream_is_an_error_and_signed_out_is_explained() -> None:
    cut = sse(
        [{"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, "delta": "Hi"}]
    )
    p = ChatGPTProvider(FakeAuth(), ProviderSettings(model="m"), http_client=streaming(cut))
    with pytest.raises(ProviderError, match="cut off"):
        [e async for e in p.stream(REQUEST)]
    out = ChatGPTProvider(
        FakeAuth(SignInError("signed_out", "You are not signed in to ChatGPT. Run `mentor login`.")),
        ProviderSettings(model="m"),
    )
    with pytest.raises(ProviderError) as info:
        [e async for e in out.stream(REQUEST)]
    assert info.value.kind == "not_configured" and "mentor login" in info.value.user_message()


async def test_chatgpt_model_list_keeps_listed_models_in_order() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        assert str(req.url) == f"{RESOURCE}/models" and req.headers["authorization"] == "Bearer access-xyz"
        models = [
            {"slug": "gpt-b", "display_name": "B", "visibility": "list"},
            {"slug": "hidden", "display_name": "H", "visibility": "hide"},
            {"slug": "gpt-a", "display_name": "A", "visibility": "list"},
        ]
        return httpx2.Response(200, json={"models": models})

    p = ChatGPTProvider(FakeAuth(), ProviderSettings(model="m"), http_client=client(handler))
    assert await p.list_models() == ["gpt-b", "gpt-a"]


# ---------------------------------------------------------------- wiring
def test_build_provider_needs_a_sign_in_and_a_model(mentor_env: dict[str, str]) -> None:
    cfg = Config()
    cfg.tutor.provider = "chatgpt"
    with pytest.raises(ProviderError) as info:
        build_provider(cfg)
    assert info.value.kind == "not_configured" and "mentor login" in info.value.user_message()
    KeychainStore().save(signed_in())
    with pytest.raises(ProviderError):  # no model yet
        build_provider(cfg)
    cfg.providers.chatgpt.model = "gpt-test"
    assert isinstance(build_provider(cfg), ChatGPTProvider)


def test_doctor_reports_the_chatgpt_sign_in(mentor_env: dict[str, str]) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    cfg = Config(initialized=True)
    cfg.tutor.provider = "chatgpt"
    cfg.providers.chatgpt.model = "gpt-test"
    save_config(cfg, paths.config_file)
    check = next(r for r in run_checks(paths) if r.name == "ChatGPT sign-in")
    assert check.status == "FAIL" and "mentor login" in check.fix
    KeychainStore().save(signed_in())
    check = next(r for r in run_checks(paths) if r.name == "ChatGPT sign-in")
    assert check.status == "OK" and "learner@example.com" in check.detail and USAGE_URL in check.detail


def test_status_says_the_chatgpt_plan_is_in_use() -> None:
    info = StatusInfo(
        goal=None,
        exercise=None,
        stage=None,
        mode="guided",
        observation="on",
        sync="current",
        sources={},
        provider="chatgpt",
        model="gpt-test",
        mock=False,
        permissions=[],
        busy=False,
    )
    assert info.model_words == "gpt-test · using ChatGPT plan"


def test_signed_tokens_are_scrubbed() -> None:
    token = make_id_token(aud=CLIENT, nonce=None)
    assert token not in scrub_keys(f"oops {token} leaked")
