"""Configuration loading for GramVault.

Design:
  - All configuration lives in a YAML file (default: `config.yaml` in the
    current working directory, overridable via the `GRAMVAULT_CONFIG_PATH`
    environment variable).
  - The YAML is validated into a single `Config` object via pydantic v2.
  - Individual values can be overridden with environment variables using a
    `GRAMVAULT_<SECTION>__<FIELD>` naming scheme (double underscore between
    section and field), e.g. `GRAMVAULT_SERVER__PORT=9000`.
  - Nothing else in the codebase should read `os.environ` for paths/model
    names directly, and nothing should hardcode a path — always go through
    `get_config()`.

Usage:
    from gramvault.config import get_config

    config = get_config()
    db_path = config.resolved_db_path
"""

from __future__ import annotations

import contextlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

# Environment variable that points at the YAML config file to load.
CONFIG_PATH_ENV_VAR = "GRAMVAULT_CONFIG_PATH"
# Prefix + delimiter scheme for per-field environment variable overrides.
ENV_PREFIX = "GRAMVAULT_"
ENV_NESTING_DELIMITER = "__"

# Default location, relative to the current working directory. GramVault is
# meant to be run from the repo root (or wherever `config.yaml` lives) —
# this is a discovery convention, not a hardcoded data path.
DEFAULT_CONFIG_FILENAME = "config.yaml"
# Sibling file for everything the Models settings page manages — provider
# routing (`ai:` / `providers:`), API keys, and the auth token. Deep-merged
# over config.yaml at load time so config.yaml stays hand-authored and
# fully commented. Gitignored; chmod 600 (it holds credentials). You can
# still put `ai:` / `providers:` in config.yaml by hand if you prefer.
SECRETS_FILENAME = "secrets.yaml"


class PathsConfig(BaseModel):
    """Filesystem locations. All relative paths are resolved against the
    current working directory at access time (see `Config.resolved_*`)."""

    library_dir: str = "./library"
    db_path: str = "./data/gramvault.db"
    chroma_dir: str = "./data/chroma"
    obsidian_vault_dir: str | None = None


class ModelsConfig(BaseModel):
    """Ollama model names, referenced by name only. Nothing in this
    scaffold calls these models — see Agents A3 (AI pipeline) and A4
    (chat) for the actual Ollama client calls."""

    chat_model: str = "llama3.1:8b"
    vision_model: str = "llava:7b"
    embedding_model: str = "nomic-embed-text"


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"


class ChunkingConfig(BaseModel):
    chunk_size: int = 512
    chunk_overlap: int = 64


class VideoConfig(BaseModel):
    """ffmpeg keyframe extraction settings."""

    keyframe_interval_seconds: int = 5
    max_keyframes: int = 6


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class ExportConfig(BaseModel):
    """Obsidian export behavior (Agent A6). Additive section — safe
    defaults so existing config.yaml files without an `export:` block
    keep working unchanged."""

    # Subfolder created inside `paths.obsidian_vault_dir` when a request
    # doesn't specify one explicitly.
    default_vault_subfolder: str = "GramVault"
    # "copy": copy media files into the vault subfolder and embed them
    # with Obsidian `![[...]]` syntax. "link": leave media where it is
    # and link out to the original path instead.
    media_mode: Literal["copy", "link"] = "copy"
    # Note foldering inside the export subfolder. "flat": every note in one
    # folder. "by-category": `<category>/<note>.md`. "by-date":
    # `<YYYY-MM>/<note>.md`. A re-export moves notes when this (or an
    # item's category) changes.
    layout: Literal["flat", "by-category", "by-date"] = "flat"


ProviderKind = Literal["ollama", "openai", "anthropic"]

# Tasks that can be routed to a provider independently.
AI_TASKS = ("chat", "vision", "embedding", "categorize", "digest")


class ProviderConfig(BaseModel):
    """One model backend. API keys are NOT stored here — they come from
    `secrets.yaml` (merged in at load time) or the `api_key_env`
    environment variable."""

    kind: ProviderKind = "ollama"
    # Endpoint override: the Ollama host, or an OpenAI-compatible base URL
    # (OpenRouter/Groq/vLLM/LM Studio/...). None -> the kind's default.
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None


class TaskModelConfig(BaseModel):
    provider: str  # key into Config.providers
    model: str


class AIConfig(BaseModel):
    """Per-task provider/model selection. A task left None falls back to
    the legacy flat `models:` / `ollama:` config (see Config.resolve_task),
    so an existing config.yaml keeps working unchanged."""

    chat: TaskModelConfig | None = None
    vision: TaskModelConfig | None = None
    embedding: TaskModelConfig | None = None
    categorize: TaskModelConfig | None = None
    digest: TaskModelConfig | None = None


class AuthConfig(BaseModel):
    """API auth. When `token` is set, every `/api/*` request must carry
    `Authorization: Bearer <token>`. Null (default) = no auth, which is
    only safe on a loopback / trusted-tailnet bind."""

    token: str | None = None


class Config(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)

    # --- convenience resolved paths (absolute, based on cwd) ---

    @property
    def resolved_library_dir(self) -> Path:
        return Path(self.paths.library_dir).expanduser().resolve()

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.paths.db_path).expanduser().resolve()

    @property
    def resolved_chroma_dir(self) -> Path:
        return Path(self.paths.chroma_dir).expanduser().resolve()

    @property
    def resolved_obsidian_vault_dir(self) -> Path | None:
        if self.paths.obsidian_vault_dir is None:
            return None
        return Path(self.paths.obsidian_vault_dir).expanduser().resolve()

    # --- AI task -> (provider, model) resolution ---

    def resolve_task(self, task: str) -> tuple[ProviderConfig, str]:
        """Return `(ProviderConfig, model_name)` for an AI task
        ('chat' | 'vision' | 'embedding' | 'categorize' | 'digest').

        Uses the `ai:` block when it specifies the task; otherwise falls
        back to the legacy flat config so an old config.yaml still works:
        `chat`/`categorize`/`digest` -> `models.chat_model`,
        `vision` -> `models.vision_model`, `embedding` ->
        `models.embedding_model`, all on the `ollama` host.
        """
        if task not in AI_TASKS:
            raise ValueError(f"unknown AI task {task!r}")

        selection: TaskModelConfig | None = getattr(self.ai, task)
        if selection is not None:
            provider = self.providers.get(selection.provider)
            if provider is None:
                # A provider name with no `providers:` entry: assume a
                # local Ollama at the default host (the common "I just
                # named it" case).
                provider = ProviderConfig(kind="ollama")
            return provider, selection.model

        legacy_model = {
            "chat": self.models.chat_model,
            "categorize": self.models.chat_model,
            "digest": self.models.chat_model,
            "vision": self.models.vision_model,
            "embedding": self.models.embedding_model,
        }[task]
        return ProviderConfig(kind="ollama", base_url=self.ollama.host), legacy_model


def _find_config_path() -> Path | None:
    """Locate the YAML config file to load, or None to use pure defaults."""
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)

    cwd_candidate = Path.cwd() / DEFAULT_CONFIG_FILENAME
    if cwd_candidate.exists():
        return cwd_candidate

    return None


def _set_nested(data: dict[str, Any], dotted_keys: list[str], value: str) -> None:
    """Set `value` into nested dict `data` following `dotted_keys`, coercing
    the value to int/float/bool where it obviously parses as one (YAML-ish
    coercion), otherwise leaving it as a string for pydantic to validate."""
    cursor = data
    for key in dotted_keys[:-1]:
        cursor = cursor.setdefault(key, {})
    leaf_key = dotted_keys[-1]

    parsed: Any = value
    lowered = value.lower()
    if lowered in ("true", "false"):
        parsed = lowered == "true"
    elif lowered in ("null", "none", "~"):
        parsed = None
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value

    cursor[leaf_key] = parsed


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply GRAMVAULT_<SECTION>__<FIELD> environment variable overrides
    on top of the loaded YAML data (mutates and returns `data`)."""
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        # Skip the config-path variable itself.
        if env_key == CONFIG_PATH_ENV_VAR:
            continue
        remainder = env_key[len(ENV_PREFIX) :]
        if not remainder:
            continue
        parts = [p.lower() for p in remainder.split(ENV_NESTING_DELIMITER) if p]
        if not parts:
            continue
        _set_nested(data, parts, env_value)
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` into `base` (mutating `base`)."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_secrets(config_path: Path | None) -> dict[str, Any]:
    """Read `secrets.yaml` sitting beside `config.yaml`, if present.

    Shape mirrors the parts of Config that hold credentials:
        providers: {anthropic: {api_key: sk-...}}
        auth: {token: ...}
    """
    if config_path is None:
        return {}
    secrets_path = config_path.parent / SECRETS_FILENAME
    if not secrets_path.exists():
        return {}
    with secrets_path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded if isinstance(loaded, dict) else {}


def _resolve_provider_key_envs(raw: dict[str, Any]) -> None:
    """For any provider with `api_key_env` set and no explicit `api_key`,
    pull the key from that environment variable."""
    for provider in (raw.get("providers") or {}).values():
        if not isinstance(provider, dict):
            continue
        env_name = provider.get("api_key_env")
        if env_name and not provider.get("api_key") and os.environ.get(env_name):
            provider["api_key"] = os.environ[env_name]


def load_config(path: Path | None = None) -> Config:
    """Load configuration from YAML (if present) + `secrets.yaml` +
    environment overrides.

    This does NOT cache — use `get_config()` for the cached singleton.
    Passing an explicit `path` is mainly useful for tests.
    """
    config_path = path if path is not None else _find_config_path()

    raw: dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            if loaded:
                raw = loaded

    _deep_merge(raw, _load_secrets(config_path))
    raw = _apply_env_overrides(raw)
    _resolve_provider_key_envs(raw)
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return the process-wide cached Config singleton."""
    return load_config()


def reload_config() -> Config:
    """Clear the cached singleton and reload it. Mainly for tests / after
    a config file is edited at runtime."""
    get_config.cache_clear()
    return get_config()


def get_config_path() -> Path:
    """Resolve which config.yaml file writes should target — same discovery
    rule as `load_config()`. Kept as a separate accessor (rather than
    stashing this on `Config` itself) so `Config` stays a plain,
    serializable data model with no filesystem provenance baked in, and so
    it's overridable per-app the same way `get_config_dependency` is (see
    `gramvault.api.deps.get_config_path_dependency`) — tests must never
    resolve this to the real project's config.yaml."""
    return _find_config_path() or (Path.cwd() / DEFAULT_CONFIG_FILENAME)


def get_secrets_path() -> Path:
    """Where `secrets.yaml` writes should target — beside `config.yaml`."""
    return get_config_path().parent / SECRETS_FILENAME


def update_secrets(patch: dict[str, Any], config_path: Path) -> Config:
    """Deep-merge `patch` into the `secrets.yaml` beside `config_path`
    (creating it, `chmod 600`), then return the freshly loaded config.
    `patch` mirrors the Config shape, e.g. `{"ai": {"chat": {...}}}` or
    `{"providers": {"anthropic": {"api_key": "sk-..."}}}`; a leaf value of
    `None` deletes that key. `config_path` is passed explicitly (see
    `get_config_path`) so tests can point this at a tmp file."""
    secrets_path = config_path.parent / SECRETS_FILENAME
    current: dict[str, Any] = {}
    if secrets_path.exists():
        loaded = yaml.safe_load(secrets_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded

    def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
        for key, value in overlay.items():
            if value is None:
                base.pop(key, None)
            elif isinstance(value, dict) and isinstance(base.get(key), dict):
                _merge(base[key], value)
            else:
                base[key] = value

    _merge(current, patch)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(
        yaml.safe_dump(current, sort_keys=True, default_flow_style=False), encoding="utf-8"
    )
    with contextlib.suppress(OSError):  # e.g. Windows
        secrets_path.chmod(0o600)

    get_config.cache_clear()
    return load_config(config_path)


def save_obsidian_vault_dir(vault_dir: str | None, config_path: Path) -> Config:
    """Persist `paths.obsidian_vault_dir` to `config_path` and refresh the
    cached singleton, so the value survives a server restart.

    Rewrites just the `obsidian_vault_dir` line in place with a targeted
    regex rather than a full YAML load/dump round-trip, because PyYAML's
    dumper silently drops the file's comments (config.yaml is meant to stay
    human-readable/hand-editable). Used by the Settings page's "Save vault
    path" action, since the export endpoints only ever read from config,
    never from a request body. Callers must pass an explicit `config_path`
    (see `get_config_path`) rather than relying on discovery here, so tests
    can point this at a tmp file instead of the real config.yaml.
    """
    # A hand-rolled double-quoted YAML scalar, not yaml.safe_dump(vault_dir)
    # — PyYAML appends a spurious "...\n" document-end marker when dumping a
    # bare top-level string, which corrupts the file once spliced inline.
    value_literal = "null" if vault_dir is None else '"' + vault_dir.replace("\\", "\\\\").replace('"', '\\"') + '"'
    new_line = f"  obsidian_vault_dir: {value_literal}"

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else "paths:\n"

    # Replacement strings built from a filesystem path (esp. on Windows,
    # where "\U..."/"\1" etc. look like regex backreferences/escapes) must
    # go through a callable, never a plain string, or re.sub tries to parse
    # backslashes in the path as its own escape syntax.
    pattern = re.compile(r"^  obsidian_vault_dir:.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda _: new_line, text, count=1)
    elif re.search(r"^paths:", text, re.MULTILINE):
        text = re.sub(
            r"^paths:$", lambda _: "paths:\n" + new_line, text, count=1, flags=re.MULTILINE
        )
    else:
        text = text.rstrip("\n") + "\n\npaths:\n" + new_line + "\n"

    config_path.write_text(text, encoding="utf-8")

    # Clear the global singleton on a best-effort basis so a real running
    # server picks up the change on its next get_config() call. Note this
    # does NOT re-derive from config_path — get_config() re-runs its own
    # discovery (_find_config_path()), which matches config_path exactly
    # when this is called via the real dependency (get_config_path), but
    # may diverge under a test's dependency override. The return value
    # below is always correct regardless, since it loads config_path
    # directly rather than relying on discovery.
    get_config.cache_clear()
    return load_config(config_path)


def save_export_settings(
    config_path: Path, *, layout: str | None = None, media_mode: str | None = None
) -> Config:
    """Persist `export.layout` / `export.media_mode` to `config_path` with
    the same targeted line-rewrite approach as `save_obsidian_vault_dir`
    (keeps the file's comments). Adds an `export:` block if absent."""
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    has_export_block = bool(re.search(r"^export:\s*$", text, re.MULTILINE))

    for key, value in (("layout", layout), ("media_mode", media_mode)):
        if value is None:
            continue
        new_line = f"  {key}: {value}"
        line_pattern = re.compile(rf"^  {key}:.*$", re.MULTILINE)
        if has_export_block and line_pattern.search(text):
            text = line_pattern.sub(lambda _, repl=new_line: repl, text, count=1)
        elif has_export_block:
            text = re.sub(
                r"^export:\s*$",
                lambda _, repl=new_line: f"export:\n{repl}",
                text,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            text = text.rstrip("\n") + f"\n\nexport:\n{new_line}\n"
            has_export_block = True

    config_path.write_text(text, encoding="utf-8")
    get_config.cache_clear()
    return load_config(config_path)
