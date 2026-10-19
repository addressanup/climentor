"""`mentor init`: confirm the profile, create the practice folder, pick a provider.

There are two front ends. People at a terminal get the full-screen setup in
``ui/setup.py``. Scripts, ``--yes`` and tests use the plain questions in
``run_init``. Both collect a ``SetupAnswers`` and save it with
``apply_answers``, so they behave the same.
"""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .apikeys import ENV_VARS, get_api_key, set_api_key
from .config import Config, ProviderName, save_config
from .db.store import Store, open_store
from .memory.profile import INTERVENTION_CHOICES, INTERVENTION_KEY, Profile
from .paths import AppPaths
from .permissions import Capability, PermissionManager, Scope
from .providers import build_provider
from .providers.base import ProviderError

if TYPE_CHECKING:
    from .chatgpt_auth import ChatGPTAuth

Ask = Callable[[str], str]
DEFAULT_HOSTS = {"anthropic": "api.anthropic.com", "openai": "api.openai.com", "chatgpt": "ChatGPT (api.openai.com)"}


@dataclass
class SetupAnswers:
    """Everything setup asks. Nothing is saved until ``apply_answers``."""

    workspace: str
    facts: dict[str, str | None] = field(default_factory=dict)  # key -> kept (maybe edited) text; None = removed
    intervention: str | None = None  # None = leave as it is
    own_words: str = ""
    remember_observation: bool = False
    provider: ProviderName = "mock"
    base_url: str | None = None  # None = leave as configured
    model: str = ""
    allow_network: bool = False


def proposed_facts(profile: Profile) -> list[tuple[str, str]]:
    """Starting guesses about the learner that still wait for a yes, no, or edit."""
    profile.seed_defaults()
    return [
        (f.key, f.value)
        for f in profile.facts(include_rejected=True)
        if f.provenance == "default_proposed" and f.status == "proposed"
    ]


def server_address(config: Config, provider: ProviderName) -> str:
    """Where a provider's requests go, for messages like “Asking api.deepseek.com…”."""
    return config.provider_settings(provider).base_url or DEFAULT_HOSTS.get(provider, provider)


async def fetch_models(config: Config, provider: ProviderName) -> list[str]:
    """Ask the provider which models it offers. Raises ``ProviderError`` with a clear message."""
    settings = config.provider_settings(provider)
    previous = settings.model
    settings.model = settings.model or "placeholder"
    try:
        client = build_provider(config, provider)
        try:
            return await client.list_models()
        finally:
            await client.aclose()
    finally:
        settings.model = previous


def apply_answers(paths: AppPaths, config: Config, answers: SetupAnswers, store: Store) -> Config:
    """Create the folder, record the profile and approvals, and write config.toml."""
    root = Path(answers.workspace).expanduser()
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
    config.workspace_root = str(root)

    profile = Profile(store)
    suggested = dict(proposed_facts(profile))
    for key, text in answers.facts.items():
        if text is None:
            profile.reject(key)
        elif text.strip() == suggested.get(key):
            profile.confirm(key)
        else:
            profile.set(key, text)
    if answers.intervention is not None:
        profile.set(
            INTERVENTION_KEY, answers.intervention if answers.intervention in INTERVENTION_CHOICES else "balanced"
        )
    if answers.own_words.strip():
        profile.set("goal.own_words", answers.own_words.strip())

    pm = PermissionManager(store, "init")  # approvals can be revoked later with /permissions revoke
    if answers.remember_observation:
        pm.grant(Capability.OBSERVE_TERMINAL, Scope.PERSISTENT)
        for cap in (Capability.OBSERVE_EDITOR, Capability.READ_FILES, Capability.RUN_CHECKS):
            pm.grant(cap, Scope.PERSISTENT, root)

    config.tutor.provider = answers.provider
    settings = config.provider_settings(answers.provider)
    if answers.base_url is not None:
        settings.base_url = answers.base_url
    if answers.model:
        settings.model = answers.model
    if answers.provider != "mock" and answers.allow_network:
        pm.grant(Capability.NETWORK, Scope.PERSISTENT, answers.provider)
    config.initialized = True
    save_config(config, paths.config_file)
    return config


# ---------------------------------------------------------------- plain questions (scripts, --yes, tests)
def _yes(answer: str, default: bool = False) -> bool:
    a = answer.strip().lower()
    return default if not a else a in ("y", "yes")


def run_init(
    paths: AppPaths,
    config: Config,
    *,
    assume_yes: bool = False,
    provider: ProviderName | None = None,
    model: str | None = None,
    workspace: str | None = None,
    base_url: str | None = None,
    ask: Ask = input,
    say: Callable[[str], None] = print,
    auth: ChatGPTAuth | None = None,
) -> Config:
    paths.ensure()
    store = open_store(paths.db_file)
    try:
        say("CLI Mentor setup. Nothing is installed; only files under the folders shown are created.\n")
        answers = SetupAnswers(workspace=str(Path(workspace or config.workspace_root).expanduser()), base_url=base_url)

        # 1. Practice folder
        root = Path(answers.workspace)
        if not root.exists():
            prompt = f"Create the practice folder {root}? It is the only folder the tutor reads by default. [y/N] "
            if not (assume_yes or _yes(ask(prompt))):
                say("  Not created. Run `mentor init` again when you are ready.")
                return config

        # 2. Profile: seeded as proposals, confirmed or edited by the learner.
        say("\nYour starting profile. These are suggestions until you confirm them.")
        for key, value in proposed_facts(Profile(store)):
            reply = "y" if assume_yes else ask(f"  “{value}”  [Y]es / [n]o / [e]dit: ").strip().lower()
            if reply in ("", "y", "yes"):
                answers.facts[key] = value
            elif reply.startswith("e"):
                answers.facts[key] = ask("    Your version: ").strip() or value
            else:
                answers.facts[key] = None
        if not assume_yes:
            style = ask("How often may the tutor speak without being asked? [quiet/BALANCED/proactive] ")
            answers.intervention = style.strip().lower() or "balanced"
            answers.own_words = ask("Anything else you want the tutor to know? (Enter to skip) ")

        # 3. Observation approval
        answers.remember_observation = assume_yes or _yes(
            ask(
                f"\nRemember approval to observe your session panes and read files in {root}? "
                "(If no, the tutor asks at each start.) [y/N] "
            )
        )

        # 4. Provider
        chosen: ProviderName = provider or "mock"
        if provider is None and not assume_yes:
            reply = ask(
                "\nTutor model: [M]ock (offline demo, not an AI) / chatgpt (your ChatGPT plan) / anthropic / openai: "
            )
            reply = reply.strip().lower()
            chosen = reply if reply in ("chatgpt", "anthropic", "openai") else "mock"  # type: ignore[assignment]
        answers.provider = chosen
        if base_url is not None:
            config.provider_settings(chosen).base_url = base_url
        if chosen == "chatgpt":
            _chatgpt_sign_in(paths, auth, assume_yes, ask, say)
        elif chosen != "mock" and get_api_key(chosen).key is None:
            if assume_yes:
                say(f"  No API key found. Set {ENV_VARS[chosen]} or run `mentor key set {chosen}`.")
            else:
                key = getpass.getpass(f"  Paste your {chosen} API key (hidden; stored in the macOS Keychain): ").strip()
                if key:
                    set_api_key(chosen, key)
                    say("  Saved to Keychain.")
        elif chosen != "mock":
            say(f"  Using the {chosen} key from {get_api_key(chosen).source} (not shown).")
        if chosen != "mock":
            if model:
                answers.model = model
            elif not assume_yes:
                answers.model = _choose_model(config, chosen, ask, say) or ""
            if not (answers.model or config.provider_settings(chosen).model):
                say("  No model chosen yet. Re-run `mentor init` later.")
            answers.allow_network = assume_yes or _yes(
                ask(f"Allow sending tutor context to {chosen} (remembered; revoke any time)? [y/N] ")
            )
        apply_answers(paths, config, answers, store)
        say(f"\nSaved {paths.config_file}. Next: `mentor doctor`, then `mentor start`.")
        return config
    finally:
        store.close()


def _chatgpt_sign_in(
    paths: AppPaths, auth: ChatGPTAuth | None, assume_yes: bool, ask: Ask, say: Callable[[str], None]
) -> None:
    from .chatgpt_auth import PLAN_OFF, USAGE_URL, ChatGPTAuth, SignInError

    auth = auth or ChatGPTAuth(paths)
    account = auth.account()
    if account is not None and account.signed_in and account.plan_enabled:
        say(f"  Signed in to ChatGPT as {account.label}.")
        return
    if assume_yes or not _yes(ask("  Continue with ChatGPT? Your browser opens so you can sign in. [Y/n] "), True):
        say("  Not signed in to ChatGPT yet. Run `mentor login` before `mentor start`.")
        return
    try:
        account = asyncio.run(
            auth.sign_in(
                lambda url: say(f"  If the browser does not open, use this link:\n  {url}\n  Waiting for you…")
            )
        )
    except SignInError as exc:
        say(f"  Sign-in did not finish: {exc.message}")
        return
    if account.plan_enabled:
        say(f"  You're using your ChatGPT plan ({account.label}). Manage usage: {USAGE_URL}")
    else:
        say(f"  {PLAN_OFF}")


def _choose_model(config: Config, provider: ProviderName, ask: Ask, say: Callable[[str], None]) -> str | None:
    where = server_address(config, provider)
    if not _yes(ask(f"  Fetch the list of models from {where}? (signs in there) [Y/n] "), True):
        return ask("  Type a model id: ").strip() or None
    try:
        models = asyncio.run(fetch_models(config, provider))
    except ProviderError as exc:
        say(f"  {exc.user_message() if provider == 'chatgpt' else exc.message}")
        if exc.kind == "auth" and provider != "chatgpt":
            say("  Check two things: the key belongs to this service, and the address above is right.")
            say("  For an OpenAI-compatible service, run init with --base-url <its address>.")
        return ask("  Type a model id (or press Ctrl+C to stop; nothing is saved to config yet): ").strip() or None
    for i, m in enumerate(models[:40], start=1):
        say(f"   {i:2}. {m}")
    answer = ask("  Pick a number or type a model id: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    return answer or None
