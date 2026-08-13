"""Configuration and paths for OBS Agent."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_CODEBASE_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_VAULT = Path.home() / "Documents" / "obs-vault"
_DEFAULT_TELEGRAM_TEMP_ROOT = Path("/tmp") / "obs-agent"
_DEFAULT_TELEGRAM_STATE_DB_PATH = (
    _DEFAULT_CODEBASE_ROOT / ".obs-agent" / "state" / "telegram-state.sqlite3"
)
_DEFAULT_TELEGRAM_TRANSCRIPTION_SCRIPT = _DEFAULT_CODEBASE_ROOT / "examples" / "transcription" / "transcribe"
_DEFAULT_CACHE_WINDOW_SECONDS = 1000 * 60 * 60  # 1000 hours; effectively no expiry for now

# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

# Shorthand → full model name. When the user passes a shorthand like "claude"
# or "gpt", we resolve it to the latest/best model in that tier.  More specific
# strings (e.g. "gpt-5.4") pass through unchanged.  Maintained as a flat dict;
# update when new models are released.
MODEL_RESOLUTION: dict[str, str] = {
    # Anthropic tiers
    "claude": "claude-opus-5",
    "opus": "claude-opus-5",
    "claude-opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "claude-sonnet": "claude-sonnet-5",
    "fable": "claude-fable-5",
    "claude-fable": "claude-fable-5",
    "haiku": "claude-haiku-4-5",
    "claude-haiku": "claude-haiku-4-5",
    # OpenAI tiers – "gpt" resolves to main production model
    "gpt": "gpt-5.6-sol",
    "gpt-pro": "gpt-5.6-sol",
    "sol": "gpt-5.6-sol",
    "gpt-sol": "gpt-5.6-sol",
    "gpt-mini": "gpt-5.4-mini",
    "openai": "gpt-5.6-sol",
    "chatgpt": "gpt-5.6-sol",
    # Google tiers
    "gemini": "gemini-3.1-flash-lite-preview",
    "gemini-pro": "gemini-3.1-pro-preview",
    "gemini-flash": "gemini-2.5-flash",
}

# Regex for context-window suffix: [1m], [200k], [128k], etc.
_CONTEXT_SUFFIX_RE = re.compile(r"\[(\d+)([mk])\]$", re.IGNORECASE)

_DEFAULT_CONTEXT_TOKENS = 1_000_000
_DEFAULT_AUTO_COMPACT_WINDOW_TOKENS = 0
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-haiku-4-5": 200_000,
    "local-qwen3.5-27b": 32_000,
    "local-gemma4-31b": 32_000,
    "gpt-5.6-sol": 400_000,
    "gpt-5.6-luna": 400_000,
    "gpt-5.6-terra": 400_000,
    "gpt-5.5": 400_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.4-mini": 1_000_000,
}


@dataclass(frozen=True)
class ModelContext:
    """Resolved model identity plus OBS context-window metadata."""

    model: str
    context_tokens: int
    explicit_context: bool

    @property
    def model_for_claude_code(self) -> str:
        return self.model + _context_suffix_for_tokens(self.context_tokens)


def _context_suffix_for_tokens(context_tokens: int) -> str:
    if context_tokens % 1_000_000 == 0:
        return f"[{context_tokens // 1_000_000}m]"
    if context_tokens % 1_000 == 0:
        return f"[{context_tokens // 1_000}k]"
    return f"[{context_tokens}k]"


def split_context_suffix(model_str: str) -> tuple[str, int | None]:
    """Split a model string into (clean_model, explicit_context_tokens)."""
    m = _CONTEXT_SUFFIX_RE.search(model_str)
    if not m:
        return model_str.strip(), None
    value = int(m.group(1))
    unit = m.group(2).lower()
    tokens = value * 1_000_000 if unit == "m" else value * 1_000
    clean = model_str[: m.start()].strip()
    return clean, tokens


def resolve_model(shorthand: str) -> str:
    """Resolve a shorthand model name to its full identifier.

    Model identity and context window are intentionally separate.  Shorthands
    resolve on the clean model name, then any explicit context suffix is
    reattached.  Default context is added later at the Claude Code boundary.
    """
    clean, ctx_tokens = split_context_suffix(shorthand)
    resolved = MODEL_RESOLUTION.get(clean.lower().strip(), clean.strip())
    if ctx_tokens is not None:
        return resolved + _context_suffix_for_tokens(ctx_tokens)
    return resolved


def resolve_model_context(
    model_str: str,
    *,
    default_context_tokens: int = _DEFAULT_CONTEXT_TOKENS,
) -> ModelContext:
    """Resolve model identity and OBS context window as separate fields."""
    resolved = resolve_model(model_str)
    clean, ctx_tokens = split_context_suffix(resolved)
    clean = clean.strip()
    if ctx_tokens is not None:
        return ModelContext(clean, ctx_tokens, True)
    return ModelContext(
        clean,
        MODEL_CONTEXT_WINDOWS.get(clean.lower(), default_context_tokens),
        False,
    )


def context_window_for_model(
    model_str: str,
    *,
    default_context_tokens: int = _DEFAULT_CONTEXT_TOKENS,
) -> tuple[str, int, bool]:
    """Return (resolved_clean_model, context_tokens, explicit_suffix)."""
    resolved = resolve_model_context(
        model_str,
        default_context_tokens=default_context_tokens,
    )
    return resolved.model, resolved.context_tokens, resolved.explicit_context


def normalize_model_for_claude_code(
    model_str: str,
    *,
    default_context_tokens: int = _DEFAULT_CONTEXT_TOKENS,
) -> str:
    """Resolve model identity and append OBS's context window suffix.

    OBS treats context length as runtime metadata, not part of model identity.
    At the Claude Code boundary we always pass the resolved context suffix,
    including the default ``[1m]`` for native Claude models.
    """
    resolved = resolve_model_context(
        model_str,
        default_context_tokens=default_context_tokens,
    )
    return resolved.model_for_claude_code


def parse_context_suffix(model_str: str) -> tuple[str, int]:
    """Split a model string into (clean_model, context_tokens).

    Examples
    --------
    >>> parse_context_suffix("claude-opus-4-7[1m]")
    ('claude-opus-4-7', 1000000)
    >>> parse_context_suffix("gpt-5.4-mini[200k]")
    ('gpt-5.4-mini', 200000)
    >>> parse_context_suffix("gemini-3.1-flash-lite-preview")
    ('gemini-3.1-flash-lite-preview', 1000000)
    """
    resolved = resolve_model_context(model_str)
    return resolved.model, resolved.context_tokens


def compaction_threshold(context_tokens: int) -> int:
    """Return the compaction-trigger token count for a given context window.

    Reverse-engineered from Anthropic's Claude Code behaviour:
      - 200 000 tokens → fires at ~167 000  (≈83.5 %)
      - 1 000 000 tokens → fires at ~920 000 (≈92.0 %)

    The percentage scales linearly from 83.5 % at 200 K to 92.0 % at 1 M,
    clamped at those bounds.  A safety floor of 10 000 tokens below the window
    is enforced so compaction always has room to work.
    """
    if context_tokens <= 0:
        return 0
    # Linear interpolation between two known data-points
    low_ctx, low_pct = 200_000, 0.835
    high_ctx, high_pct = 1_000_000, 0.920
    if context_tokens <= low_ctx:
        pct = low_pct
    elif context_tokens >= high_ctx:
        pct = high_pct
    else:
        ratio = (context_tokens - low_ctx) / (high_ctx - low_ctx)
        pct = low_pct + ratio * (high_pct - low_pct)
    threshold = int(context_tokens * pct)
    # Ensure at least 10 K headroom
    return min(threshold, context_tokens - 10_000)


def auto_compact_window_for_context(
    context_tokens: int,
    *,
    auto_compact_window_tokens: int = _DEFAULT_AUTO_COMPACT_WINDOW_TOKENS,
) -> int:
    """Return the Claude Code auto-compact window to pass for a model context.

    The window should match the resolved model context so Claude Code's own
    compaction trigger tracks the same percentage curve as native Claude
    sessions. Operators can set ``OBS_AUTO_COMPACT_WINDOW_TOKENS`` as an
    emergency cap for a provider with a smaller observed prompt limit.
    """
    if context_tokens <= 0:
        return 0
    if auto_compact_window_tokens <= 0:
        return context_tokens
    return min(context_tokens, auto_compact_window_tokens)


def default_auto_compact_window_for_model(
    model_str: str,
    context_tokens: int,
) -> int:
    """Return OBS's built-in auto-compact window for a resolved model.

    By default, OBS passes the resolved context window through to Claude Code.
    That lets Claude Code apply its built-in compaction curve consistently:
    about 167K for a 200K window, about 342K for a 400K window, and about
    920K for a 1M window.
    """
    if context_tokens <= 0:
        return 0
    return context_tokens


def auto_compact_window_for_model(
    model_str: str,
    context_tokens: int,
    *,
    auto_compact_window_tokens: int = _DEFAULT_AUTO_COMPACT_WINDOW_TOKENS,
) -> int:
    """Return the Claude Code auto-compact window for a model/context pair.

    ``auto_compact_window_tokens`` is an explicit operator override. A value of
    0 means "use OBS's model-aware default", not "disable the conservative
    proxied-provider window".
    """
    if context_tokens <= 0:
        return 0
    if auto_compact_window_tokens > 0:
        return min(context_tokens, auto_compact_window_tokens)
    return default_auto_compact_window_for_model(model_str, context_tokens)


def is_claude_model(model: str) -> bool:
    """Return True if *model* is an Anthropic Claude model."""
    clean = model.split("[")[0].strip().lower()
    return clean.startswith("claude") or resolve_model(clean).startswith("claude")


IMMUTABLE_PATTERNS: list[str] = [
    "Misc/Meeting Notes",
]


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    normalized_path = _resolved(path)
    normalized_parent = _resolved(parent)
    return normalized_path == normalized_parent or normalized_parent in normalized_path.parents


@dataclass
class OBSConfig:
    """Central configuration for OBS Agent."""

    vault_path: Path = field(default_factory=lambda: _DEFAULT_VAULT)
    model: str = "gpt-5.6-sol"
    # Shorthand default model used when OBS_AGENT_MODEL is not set.
    # Resolved via MODEL_RESOLUTION (e.g. "sol" → "gpt-5.6-sol");
    # full model names pass through unchanged.
    # Change this to e.g. "claude" to make root sessions default to Claude.
    default_model: str = "sol"
    claude_dir: str = ".claude"
    agent_entry_file: str = "CLAUDE.md"
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 7832
    cache_window_seconds: int = _DEFAULT_CACHE_WINDOW_SECONDS
    max_queue_continuations: int = 3
    bg_fork_timeout: float = 600.0  # seconds to wait for background forks
    max_buffer_size: int = 10 * 1024 * 1024  # 10 MB SDK JSON buffer limit
    context_window_estimate_tokens: int = 400_000
    auto_compact_window_tokens: int = _DEFAULT_AUTO_COMPACT_WINDOW_TOKENS
    context_probe_claude_cli: bool = False
    claude_idle_process_cap: int | None = None
    claude_kill_on_idle: bool = False
    fork_cache_warmup_delay_seconds: float = 1.0

    # Cache proxy
    cache_proxy_port: int = 18923
    cache_proxy_enabled: bool = True

    # CLI proxy (CLIProxyAPI — routes non-Anthropic models)
    cli_proxy_base_url: str = "http://127.0.0.1:8317"
    cli_proxy_api_key: str = "sk-anything"  # Must match api-keys in cliproxyapi.conf

    # Telegram
    telegram_bot_token: str | None = None
    telegram_bot_tokens: list[str] = field(default_factory=list)
    telegram_userbot_api_id: int | None = None
    telegram_userbot_api_hash: str | None = None
    telegram_userbot_session: str | None = None
    telegram_group_folder_title: str | None = None
    telegram_group_addlist_url: str | None = None
    telegram_allowed_user_ids: list[int] = field(default_factory=list)
    telegram_notify_username: str | None = None
    telegram_temp_root: Path = field(default_factory=lambda: _DEFAULT_TELEGRAM_TEMP_ROOT)
    telegram_state_db_path: Path = field(default_factory=lambda: _DEFAULT_TELEGRAM_STATE_DB_PATH)
    telegram_state_retention_days: int = 30
    telegram_transcription_script: Path = field(
        default_factory=lambda: _DEFAULT_TELEGRAM_TRANSCRIPTION_SCRIPT
    )
    telegram_transport_base_chat_interval_seconds: float = 0.35
    telegram_transport_max_chat_interval_seconds: float = 5.0
    telegram_typing_action_interval_seconds: float = 4.0
    telegram_typing_actions_enabled: bool = True

    # --- Class Methods ---

    @classmethod
    def from_env(cls) -> OBSConfig:
        """Build config from environment variables, falling back to defaults."""
        kwargs: dict = {}

        if vault := os.environ.get("OBS_VAULT_PATH"):
            kwargs["vault_path"] = Path(vault)
        if default_model := os.environ.get("OBS_DEFAULT_MODEL"):
            kwargs["default_model"] = default_model.strip()
        if entry_file := os.environ.get("OBS_AGENT_ENTRY_FILE"):
            kwargs["agent_entry_file"] = entry_file.strip() or "CLAUDE.md"
        if model := os.environ.get("OBS_AGENT_MODEL") or os.environ.get("OBS_MODEL"):
            kwargs["model"] = resolve_model(model.strip())
        else:
            dm = kwargs.get("default_model", "sol")
            kwargs["model"] = resolve_model(dm)
        if host := os.environ.get("OBS_DAEMON_HOST"):
            kwargs["daemon_host"] = host
        if port := os.environ.get("OBS_DAEMON_PORT"):
            kwargs["daemon_port"] = int(port)
        if window := os.environ.get("OBS_CACHE_WINDOW"):
            kwargs["cache_window_seconds"] = int(window)
        if max_cont := os.environ.get("OBS_MAX_QUEUE_CONTINUATIONS"):
            kwargs["max_queue_continuations"] = int(max_cont)
        if bg_timeout := os.environ.get("OBS_BG_FORK_TIMEOUT"):
            kwargs["bg_fork_timeout"] = float(bg_timeout)
        if buf_size := os.environ.get("OBS_MAX_BUFFER_SIZE"):
            kwargs["max_buffer_size"] = int(buf_size)
        if context_est := os.environ.get("OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"):
            kwargs["context_window_estimate_tokens"] = int(context_est)
        if compact_window := os.environ.get("OBS_AUTO_COMPACT_WINDOW_TOKENS"):
            kwargs["auto_compact_window_tokens"] = int(compact_window)
        if probe_cli := os.environ.get("OBS_CONTEXT_PROBE_CLAUDE_CLI"):
            kwargs["context_probe_claude_cli"] = probe_cli.strip().lower() in {"1", "true", "yes", "on"}
        if idle_cap := os.environ.get("OBS_CLAUDE_IDLE_PROCESS_CAP"):
            kwargs["claude_idle_process_cap"] = int(idle_cap)
        if kill_on_idle := os.environ.get("OBS_CLAUDE_KILL_ON_IDLE"):
            kwargs["claude_kill_on_idle"] = kill_on_idle.strip().lower() in {"1", "true", "yes", "on"}
        if fork_cache_delay := os.environ.get("OBS_FORK_CACHE_WARMUP_DELAY_SECONDS"):
            kwargs["fork_cache_warmup_delay_seconds"] = float(fork_cache_delay)
        if proxy_port := os.environ.get("OBS_CACHE_PROXY_PORT"):
            kwargs["cache_proxy_port"] = int(proxy_port)
        if proxy_enabled := os.environ.get("OBS_CACHE_PROXY_ENABLED"):
            kwargs["cache_proxy_enabled"] = proxy_enabled.strip().lower() in {"1", "true", "yes", "on"}
        if cli_proxy_url := os.environ.get("OBS_CLI_PROXY_BASE_URL"):
            kwargs["cli_proxy_base_url"] = cli_proxy_url.strip()
        if cli_proxy_key := os.environ.get("OBS_CLI_PROXY_API_KEY"):
            kwargs["cli_proxy_api_key"] = cli_proxy_key.strip()
        raw_tokens = (os.environ.get("OBS_TELEGRAM_BOT_TOKENS") or "").strip()
        if raw_tokens:
            kwargs["telegram_bot_tokens"] = [
                token.strip() for token in raw_tokens.split(",") if token.strip()
            ]
        if tg_token := os.environ.get("OBS_TELEGRAM_BOT_TOKEN"):
            kwargs["telegram_bot_token"] = tg_token.strip()
        if userbot_api_id := os.environ.get("OBS_TELEGRAM_USERBOT_API_ID"):
            kwargs["telegram_userbot_api_id"] = int(userbot_api_id)
        if userbot_api_hash := os.environ.get("OBS_TELEGRAM_USERBOT_API_HASH"):
            kwargs["telegram_userbot_api_hash"] = userbot_api_hash.strip()
        if userbot_session := os.environ.get("OBS_TELEGRAM_USERBOT_SESSION"):
            kwargs["telegram_userbot_session"] = userbot_session.strip()
        if tg_folder_title := os.environ.get("OBS_TELEGRAM_GROUP_FOLDER_TITLE"):
            kwargs["telegram_group_folder_title"] = tg_folder_title.strip() or None
        if tg_addlist_url := os.environ.get("OBS_TELEGRAM_GROUP_ADDLIST_URL"):
            kwargs["telegram_group_addlist_url"] = tg_addlist_url.strip() or None
        if tg_users := os.environ.get("OBS_TELEGRAM_ALLOWED_USERS"):
            kwargs["telegram_allowed_user_ids"] = [
                int(uid.strip()) for uid in tg_users.split(",") if uid.strip()
            ]
        if tg_username := os.environ.get("OBS_TELEGRAM_NOTIFY_USERNAME"):
            kwargs["telegram_notify_username"] = tg_username.lstrip("@").strip() or None
        if tg_temp_root := os.environ.get("OBS_TELEGRAM_TEMP_ROOT"):
            kwargs["telegram_temp_root"] = Path(tg_temp_root)
        if tg_state_db := os.environ.get("OBS_TELEGRAM_STATE_DB_PATH"):
            kwargs["telegram_state_db_path"] = Path(tg_state_db)
        if tg_state_retention := os.environ.get("OBS_TELEGRAM_STATE_RETENTION_DAYS"):
            kwargs["telegram_state_retention_days"] = int(tg_state_retention)
        if tg_transcribe := os.environ.get("OBS_TELEGRAM_TRANSCRIPTION_SCRIPT"):
            kwargs["telegram_transcription_script"] = Path(tg_transcribe)
        if tg_transport_base := os.environ.get("OBS_TELEGRAM_TRANSPORT_BASE_CHAT_INTERVAL_SECONDS"):
            kwargs["telegram_transport_base_chat_interval_seconds"] = float(tg_transport_base)
        if tg_transport_max := os.environ.get("OBS_TELEGRAM_TRANSPORT_MAX_CHAT_INTERVAL_SECONDS"):
            kwargs["telegram_transport_max_chat_interval_seconds"] = float(tg_transport_max)
        if tg_typing_interval := os.environ.get("OBS_TELEGRAM_TYPING_ACTION_INTERVAL_SECONDS"):
            kwargs["telegram_typing_action_interval_seconds"] = float(tg_typing_interval)
        if tg_typing_enabled := os.environ.get("OBS_TELEGRAM_TYPING_ACTIONS_ENABLED"):
            kwargs["telegram_typing_actions_enabled"] = tg_typing_enabled.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        return cls(**kwargs)

    # --- Claude Paths ---

    @property
    def claude_path(self) -> Path:
        return self.vault_path / self.claude_dir

    @property
    def context_path(self) -> Path:
        return self.vault_path / self.agent_entry_file

    @property
    def skills_dir(self) -> Path:
        return self.claude_path / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.claude_path / "memory"

    @property
    def system_dir(self) -> Path:
        return self.claude_path / "system"

    @property
    def topics_dir(self) -> Path:
        return self.claude_path / "topics"

    @property
    def drafts_dir(self) -> Path:
        return self.claude_path / "drafts"

    @property
    def memory_parent_note(self) -> Path:
        return self.claude_path / "memory.md"

    # --- Daemon ---

    @property
    def base_url(self) -> str:
        return f"http://{self.daemon_host}:{self.daemon_port}"

    # --- Immutable Paths ---

    @property
    def immutable_patterns(self) -> list[str]:
        return list(IMMUTABLE_PATTERNS)

    def is_immutable(self, path: Path) -> bool:
        """Check if a path matches any immutable pattern."""
        path_str = str(path)
        return any(pattern in path_str for pattern in IMMUTABLE_PATTERNS)

    # --- Validation ---

    @property
    def telegram_primary_bot_token(self) -> str | None:
        """Primary Telegram bot token used for polling updates."""
        if self.telegram_bot_token:
            return self.telegram_bot_token
        if self.telegram_bot_tokens:
            return self.telegram_bot_tokens[0]
        return None

    @property
    def telegram_sender_bot_tokens(self) -> list[str]:
        """Ordered deduplicated sender token list (primary first)."""
        ordered: list[str] = []
        primary = self.telegram_primary_bot_token
        if primary:
            ordered.append(primary)
        for token in self.telegram_bot_tokens:
            if token and token not in ordered:
                ordered.append(token)
        return ordered

    def validate(self) -> None:
        """Validate that expected vault structure exists. Raises FileNotFoundError."""
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Vault not found: {self.vault_path}")
        if not self.claude_path.is_dir():
            raise FileNotFoundError(f".claude directory not found: {self.claude_path}")
        if not _is_within(self.context_path, self.vault_path):
            raise ValueError(f"Agent entry file must be inside vault: {self.context_path}")
        if not self.context_path.is_file():
            raise FileNotFoundError(f"Agent entry file not found: {self.context_path}")
        if _is_within(self.telegram_state_db_path, self.telegram_temp_root):
            raise ValueError(
                "Invalid Telegram state DB path: OBS_TELEGRAM_STATE_DB_PATH must be outside "
                "OBS_TELEGRAM_TEMP_ROOT to avoid startup cleanup deleting persistence data."
            )
