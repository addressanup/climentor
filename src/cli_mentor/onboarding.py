"""`mentor init`: confirm the profile, create the practice folder, pick a provider."""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable
from pathlib import Path

from .apikeys import ENV_VARS, get_api_key, set_api_key
from .config import Config, ProviderName, save_config
from .db.store import open_store
from .memory.profile import INTERVENTION_CHOICES, INTERVENTION_KEY, Profile
from .paths import AppPaths
from .permissions import Capability, PermissionManager, Scope
from .providers import build_provider
from .providers.base import ProviderError

Ask = Callable[[str], str]


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
) -> Config:
    paths.ensure()
    store = open_store(paths.db_file)
    profile = Profile(store)
    say("CLI Mentor setup. Nothing is installed; only files under the folders shown are created.\n")

    # 1. Practice folder
    root = Path(workspace or config.workspace_root).expanduser()
    if not root.exists():
        prompt = f"Create the practice folder {root}? It is the only folder the tutor reads by default. [y/N] "
        if assume_yes or _yes(ask(prompt)):
            root.mkdir(parents=True, mode=0o700)
            say(f"  created {root}")
        else:
            say("  Not created. Run `mentor init` again when you are ready.")
            return config
    config.workspace_root = str(root)

    # 2. Profile: seeded as proposals, confirmed or edited by the learner.
    profile.seed_defaults()
    say("\nYour starting profile. These are suggestions until you confirm them.")
    for fact in profile.facts(include_rejected=True):
        if fact.provenance != "default_proposed" or fact.status != "proposed":
            continue
        if assume_yes:
            profile.confirm(fact.key)
            continue
        answer = ask(f"  “{fact.value}”  [Y]es / [n]o / [e]dit: ").strip().lower()
        if answer in ("", "y", "yes"):
            profile.confirm(fact.key)
        elif answer.startswith("e"):
            profile.set(fact.key, ask("    Your version: ") or fact.value)
        else:
            profile.reject(fact.key)
    if not assume_yes:
        style = ask("How often may the tutor speak without being asked? [quiet/BALANCED/proactive] ").strip().lower()
        profile.set(INTERVENTION_KEY, style if style in INTERVENTION_CHOICES else "balanced")
        goal = ask("Anything else you want the tutor to know? (Enter to skip) ").strip()
        if goal:
            profile.set("goal.own_words", goal)

    # 3. Observation approval (can be revoked later with /permissions revoke).
    pm = PermissionManager(store, "init")
    remember = assume_yes or _yes(
        ask(
            f"\nRemember approval to observe your session panes and read files in {root}? "
            "(If no, the tutor asks at each start.) [y/N] "
        )
    )
    if remember:
        for cap in (Capability.OBSERVE_TERMINAL,):
            pm.grant(cap, Scope.PERSISTENT)
        for cap in (Capability.OBSERVE_EDITOR, Capability.READ_FILES, Capability.RUN_CHECKS):
            pm.grant(cap, Scope.PERSISTENT, root)

    # 4. Provider
    chosen: ProviderName = provider or "mock"
    if provider is None and not assume_yes:
        answer = ask("\nTutor model: [M]ock (offline demo, not an AI) / anthropic / openai: ").strip().lower()
        chosen = answer if answer in ("anthropic", "openai") else "mock"  # type: ignore[assignment]
    config.tutor.provider = chosen
    settings = config.provider_settings(chosen)
    if base_url is not None:
        settings.base_url = base_url
    if chosen != "mock":
        if get_api_key(chosen).key is None:
            if assume_yes:
                say(f"  No API key found. Set {ENV_VARS[chosen]} or run `mentor key set {chosen}`.")
            else:
                key = getpass.getpass(f"  Paste your {chosen} API key (hidden; stored in the macOS Keychain): ").strip()
                if key:
                    set_api_key(chosen, key)
                    say("  Saved to Keychain.")
        else:
            say(f"  Using the {chosen} key from {get_api_key(chosen).source} (not shown).")
        if model:
            settings.model = model
        elif not assume_yes:
            settings.model = _choose_model(config, chosen, ask, say) or settings.model
        if not settings.model:
            say("  No model chosen yet. Re-run `mentor init` later.")
        if assume_yes or _yes(ask(f"Allow sending tutor context to {chosen} (remembered; revoke any time)? [y/N] ")):
            pm.grant(Capability.NETWORK, Scope.PERSISTENT, chosen)
    config.initialized = True
    save_config(config, paths.config_file)
    say(f"\nSaved {paths.config_file}. Next: `mentor doctor`, then `mentor start`.")
    store.close()
    return config


def _choose_model(config: Config, provider: ProviderName, ask: Ask, say: Callable[[str], None]) -> str | None:
    settings = config.provider_settings(provider)
    where = settings.base_url or {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}[provider]
    if not _yes(ask(f"  Fetch the list of models from {where}? (sends your key there to sign in) [Y/n] "), True):
        return ask("  Type a model id: ").strip() or None
    previous = settings.model
    settings.model = settings.model or "placeholder"
    try:
        client = build_provider(config, provider)
        models = asyncio.run(client.list_models())
    except ProviderError as exc:
        say(f"  {exc.message}")
        if exc.kind == "auth":
            say("  Check two things: the key belongs to this service, and the address above is right.")
            say("  For an OpenAI-compatible service, run init with --base-url <its address>.")
        return ask("  Type a model id (or press Ctrl+C to stop; nothing is saved to config yet): ").strip() or None
    finally:
        settings.model = previous
    for i, m in enumerate(models[:40], start=1):
        say(f"   {i:2}. {m}")
    answer = ask("  Pick a number or type a model id: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    return answer or None
