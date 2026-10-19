"""The learner's ChatGPT plan, through OpenAI's public Responses API.

Requests follow OpenAI's rules for plan usage (Sign in with ChatGPT):
``store: false`` and ``stream: true`` on every call, guidance in
``instructions``, tools grouped in a namespace, and no ``max_output_tokens``
or ``temperature``. Usage-limit errors point to ChatGPT's usage settings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, cast

import httpx2
import openai

from ..chatgpt_auth import RESOURCE, USAGE_URL, SignInError
from ..config import ProviderSettings
from .base import (
    Done,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    ToolCall,
    Usage,
    estimate_request_tokens,
    estimate_tokens,
)
from .openai_provider import map_error

log = logging.getLogger(__name__)

NAMESPACE = "mentor"
WHO = "ChatGPT"
LOGIN = "Run `mentor login` to sign in again."
# Plan-usage error codes from OpenAI's “Errors and recovery” guide.
PLAN_ERRORS: dict[str, ProviderError] = {
    "subscription_sharing_usage_limit_exceeded": ProviderError(
        "rate_limit",
        "Usage limit reached for your ChatGPT plan, or for CLI Mentor.",
        hint=f"Review your plan and app limits (Manage usage): {USAGE_URL}",
    ),
    "subscription_sharing_user_not_eligible": ProviderError(
        "auth",
        "This ChatGPT account or workspace cannot use its plan in other apps.",
        hint="Use an API key instead: run `mentor init`.",
    ),
    "subscription_sharing_usage_unavailable": ProviderError(
        "unavailable", "ChatGPT could not check your plan usage right now.", hint="Try again in a minute."
    ),
    "subscription_sharing_user_unavailable": ProviderError(
        "unavailable", "ChatGPT could not load your account right now.", hint="Try again in a minute."
    ),
    "subscription_sharing_invalid_user": ProviderError("auth", "ChatGPT did not accept your sign-in.", hint=LOGIN),
}


class TokenSource(Protocol):
    async def access_token(self) -> str: ...


def _sign_in_error(exc: SignInError) -> ProviderError:
    if exc.retryable:
        return ProviderError("unavailable", exc.message, hint="")
    return ProviderError(
        "not_configured" if exc.code in ("signed_out", "plan_not_enabled") else "auth", exc.message, hint=""
    )


def plan_error(code: str | None, status: int | None, param: str | None = None) -> ProviderError | None:
    """Turn an error code from ChatGPT plan usage into a clear message."""
    if code in PLAN_ERRORS:
        known = PLAN_ERRORS[code]
        return ProviderError(known.kind, known.message, hint=known.hint)
    if code == "subscription_sharing_unsupported_capability":
        what = f" ({param})" if param else ""
        return ProviderError(
            "bad_request",
            f"ChatGPT does not support part of this request{what}.",
            hint="Try another model: `mentor init`.",
        )
    if code and (code.startswith("chatpass_") or code == "subscription_sharing_route_not_supported"):
        return ProviderError(
            "auth", f"ChatGPT refused this request ({code}).", hint=f"{LOGIN} If it keeps happening, use an API key."
        )
    if status == 401:
        return ProviderError("auth", "ChatGPT did not accept your sign-in.", hint=LOGIN)
    if status == 403:
        return ProviderError("auth", "ChatGPT refused this request (for example, a region or account policy).", hint="")
    if status == 404:
        return ProviderError(
            "bad_request", "ChatGPT does not offer this model on your plan.", hint="Pick another with `mentor init`."
        )
    return None


def _code_of(exc: openai.APIError) -> tuple[str | None, str | None]:
    body = exc.body if isinstance(exc.body, dict) else {}
    code = (
        getattr(exc, "code", None)
        or body.get("code")
        or (body.get("error") if isinstance(body.get("error"), str) else None)
    )
    return (str(code) if code else None), (str(body["param"]) if body.get("param") else None)


class ChatGPTProvider:
    name = "chatgpt"
    makes_network_calls = True

    def __init__(
        self, auth: TokenSource, settings: ProviderSettings, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        if not settings.model:
            raise ProviderError("not_configured", "No ChatGPT model is chosen.", hint="Run `mentor init` to pick one.")
        self.auth = auth
        self.model = settings.model
        self.settings = settings
        # No redirects: a redirect could forward the learner's context to another site.
        self.http = http_client or httpx2.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=False)
        self.client = openai.AsyncOpenAI(
            api_key="replaced-per-request",
            base_url=RESOURCE,
            timeout=settings.timeout_seconds,
            max_retries=0,  # report usage limits at once instead of retrying them
            http_client=self.http,
            # The plan pays, not an API organisation: never send one from OPENAI_ORG_ID / OPENAI_PROJECT_ID.
            default_headers=cast(
                dict[str, str], {"OpenAI-Organization": openai.Omit(), "OpenAI-Project": openai.Omit()}
            ),
        )

    async def _token(self) -> str:
        try:
            return await self.auth.access_token()
        except SignInError as exc:
            raise _sign_in_error(exc) from None

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        token = await self._token()
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "instructions": request.system,
            "input": [{"role": m.role, "content": m.content} for m in request.messages],
            "store": False,
            "stream": True,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "namespace",
                    "name": NAMESPACE,
                    "description": "Actions the CLI Mentor tutor can ask the program to take.",
                    "tools": [
                        {
                            "type": "function",
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                            "strict": False,
                        }
                        for t in request.tools
                    ],
                }
            ]
        calls: list[Any] = []
        text_parts: list[str] = []
        usage: Usage | None = None
        completed = False
        try:
            stream = await self.client.with_options(api_key=token).responses.create(**kwargs)
            try:
                async for event in stream:
                    kind = getattr(event, "type", "")
                    if kind == "response.output_text.delta":
                        text_parts.append(event.delta)
                        yield TextDelta(event.delta)
                    elif kind == "response.output_item.done" and getattr(event.item, "type", "") == "function_call":
                        calls.append(event.item)
                    elif kind == "response.completed":
                        found = getattr(event.response, "usage", None)
                        if found is not None:
                            usage = Usage(found.input_tokens, found.output_tokens, False)
                        completed = True
                        break
                    elif kind in ("response.failed", "error"):
                        error = getattr(event.response, "error", None) if kind == "response.failed" else event
                        code = getattr(error, "code", None)
                        raise plan_error(code, None, getattr(error, "param", None)) or ProviderError(
                            "unknown", f"ChatGPT could not answer ({code or 'unknown error'})."
                        )
                    elif kind == "response.incomplete":
                        details = getattr(event.response, "incomplete_details", None)
                        reason = getattr(details, "reason", None) or "unknown reason"
                        raise ProviderError(
                            "unknown", f"ChatGPT stopped before finishing the reply ({reason}).", hint="Ask again."
                        )
            finally:
                await stream.close()
        except openai.APIError as exc:
            code, param = _code_of(exc)
            status = exc.status_code if isinstance(exc, openai.APIStatusError) else None
            mapped = plan_error(code, status, param)
            if mapped is None:
                mapped = map_error(exc, WHO)
                if mapped.kind == "auth":
                    mapped.hint = LOGIN
            raise mapped from None
        except openai.OpenAIError as exc:
            raise map_error(exc, WHO) from None
        if not completed:
            raise ProviderError("unavailable", "The reply from ChatGPT was cut off.", hint="Ask again.")
        for item in calls:
            try:
                args = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                log.warning("ignoring tool call with invalid JSON arguments")
                continue
            if isinstance(args, dict) and item.name:
                yield ToolCall(item.name, args, item.call_id or "")
        yield usage or Usage(estimate_request_tokens(request), estimate_tokens("".join(text_parts)), True)
        yield Done("completed")

    async def list_models(self) -> list[str]:
        """Models the learner's plan offers, in OpenAI's order."""
        token = await self._token()
        try:
            res = await self.http.get(
                f"{RESOURCE}/models", headers={"authorization": f"Bearer {token}", "accept": "application/json"}
            )
            body = res.json()
        except (httpx2.HTTPError, ValueError):
            raise ProviderError("unavailable", f"Could not reach {WHO}.") from None
        if res.status_code != 200:
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise plan_error(code, res.status_code) or ProviderError(
                "unavailable" if res.status_code >= 500 else "unknown", f"{WHO}: error (HTTP {res.status_code})."
            )
        models = body.get("models") if isinstance(body, dict) else None
        return [
            m["slug"]
            for m in models or []
            if isinstance(m, dict) and m.get("visibility") == "list" and isinstance(m.get("slug"), str) and m["slug"]
        ]

    async def aclose(self) -> None:
        await self.client.close()
        await self.http.aclose()
