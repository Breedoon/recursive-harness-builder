"""obs-e2e credcheck: validate the harness's test identities without printing secrets.

Checks (impl-plan-v3 §3 E1 WP1, design-v3 §2.2 "Credential validity", §8.10):
- every test bot token: ``getMe`` (id, username), ``getWebhookInfo`` (no webhook) and,
  with ``--holder-probe``, a zero-timeout ``getUpdates`` that must not answer
  ``409 Conflict`` (another process polling the bot). The probe passes no offset,
  so it confirms (drops) nothing;
- the test userbot session: Telethon ``get_me()`` under the run's session lock;
- the Codex/GPT sub-credentials: one tiny completion per sub-credential model
  through the existing CLIProxyAPI endpoint (a client call; its config is untouched).

Output: JSON with ids, usernames, fingerprints and ok/failed flags only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from .secrets import SecretError, bot_id_of, fingerprint, production_bot_tokens, read_env_file, resolve
from .userbot import UserbotLock, UserbotLockTimeout, make_client

BOT_API = "https://api.telegram.org"
CLIPROXY_URL = "http://127.0.0.1:8317"
CLIPROXY_KEY = "sk-anything"  # the documented public client key (config.yaml api-keys)


def check_bot(token: str, *, holder_probe: bool, client: httpx.Client) -> dict:
    out: dict = {"bot_id": bot_id_of(token), "fingerprint": fingerprint(token)}
    try:
        me = client.get(f"{BOT_API}/bot{token}/getMe", timeout=15).json()
        out["getMe_ok"] = bool(me.get("ok"))
        if me.get("ok"):
            out["username"] = me["result"].get("username")
            out["is_bot"] = me["result"].get("is_bot")
        else:
            out["getMe_error"] = me.get("description")
        hook = client.get(f"{BOT_API}/bot{token}/getWebhookInfo", timeout=15).json()
        out["webhook_empty"] = bool(hook.get("ok")) and not (hook.get("result") or {}).get("url")
        out["pending_update_count"] = (hook.get("result") or {}).get("pending_update_count")
        if holder_probe and out["webhook_empty"]:
            upd = client.post(f"{BOT_API}/bot{token}/getUpdates", json={"timeout": 0, "limit": 1}, timeout=15).json()
            out["getUpdates_ok"] = bool(upd.get("ok"))
            out["held_by_other_poller"] = upd.get("error_code") == 409
            if not upd.get("ok"):
                out["getUpdates_error"] = upd.get("description")
    except httpx.HTTPError as exc:
        out["error"] = exc.__class__.__name__
    out["valid"] = bool(out.get("getMe_ok") and out.get("is_bot") and out.get("webhook_empty")) and not out.get(
        "held_by_other_poller", False
    )
    return out


async def check_userbot(env: dict[str, str], lock_timeout: float) -> dict:
    out: dict = {}
    try:
        session = resolve("env:OBS_TEST_TELEGRAM_SESSION", env)
        api_id = resolve("env:OBS_TEST_TELEGRAM_API_ID", env)
        api_hash = resolve("env:OBS_TEST_TELEGRAM_API_HASH", env)
    except SecretError as exc:
        return {"valid": False, "error": str(exc)}
    out["session_fingerprint"] = fingerprint(session)
    lock = UserbotLock.for_session(session)
    try:
        lock.acquire(timeout=lock_timeout, owner={"by": "obs-e2e credcheck"})
    except UserbotLockTimeout as exc:
        return {**out, "valid": False, "error": str(exc)}
    client = make_client(session, int(api_id), api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        out["authorized"] = await client.is_user_authorized()
        if out["authorized"]:
            me = await client.get_me()
            out.update(
                user_id=me.id,
                is_bot=bool(me.bot),
                username=me.username,
                first_name=me.first_name,
                last_name=me.last_name,
                phone_present=bool(me.phone),
                premium=bool(getattr(me, "premium", False)),
            )
    except Exception as exc:  # report the class only; Telethon messages can echo request data
        out["error"] = exc.__class__.__name__
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            pass
        lock.release()
    allowed = {item.strip() for item in (env.get("OBS_TEST_TELEGRAM_ALLOWED_USERS") or "").split(",") if item.strip()}
    out["in_test_allowed_users"] = str(out.get("user_id")) in allowed
    out["valid"] = bool(out.get("authorized")) and out.get("is_bot") is False
    return out


def check_subcredential(model: str, client: httpx.Client, base_url: str) -> dict:
    out: dict = {"model": model, "route": "cli-proxy", "endpoint": base_url}
    try:
        resp = client.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {CLIPROXY_KEY}"},
            json={"model": model, "messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_tokens": 16},
            timeout=120,
        )
        data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        out["http_status"] = resp.status_code
        out["answered"] = bool(text.strip())
        out["total_tokens"] = (data.get("usage") or {}).get("total_tokens")
        if data.get("error"):
            out["error"] = data["error"] if isinstance(data["error"], str) else (data["error"] or {}).get("message")
    except (httpx.HTTPError, ValueError) as exc:
        out["error"] = exc.__class__.__name__
    out["valid"] = bool(out.get("answered")) and not out.get("error")
    return out


def run(args: argparse.Namespace) -> dict:
    env = read_env_file()
    prod = production_bot_tokens(env)
    report: dict = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "bots": [], "subcredentials": []}
    try:
        pool = [t.strip() for t in resolve("env:OBS_TEST_TELEGRAM_BOT_TOKENS", env).split(",") if t.strip()]
    except SecretError as exc:
        pool, report["bot_pool_error"] = [], str(exc)
    with httpx.Client() as client:
        for index, token in enumerate(pool):
            row = check_bot(token, holder_probe=args.holder_probe, client=client)
            row["index"] = index
            row["is_production_token"] = token in prod
            if row["is_production_token"]:
                row["valid"] = False
            report["bots"].append(row)
        for model in args.sub_models:
            report["subcredentials"].append(check_subcredential(model, client, args.cliproxy_url))
    if not args.skip_userbot:
        report["userbot"] = asyncio.run(check_userbot(env, args.userbot_lock_timeout))
    report["counts"] = {
        "bots_valid": sum(1 for b in report["bots"] if b["valid"]),
        "bots_total": len(report["bots"]),
        "subcredentials_valid": sum(1 for s in report["subcredentials"] if s["valid"]),
        "subcredentials_total": len(report["subcredentials"]),
        "userbot_valid": bool((report.get("userbot") or {}).get("valid")),
    }
    return report


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(prog="obs-e2e credcheck", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--holder-probe", action="store_true", help="also run a zero-timeout getUpdates (detects another poller)")
    parser.add_argument("--skip-userbot", action="store_true")
    parser.add_argument("--userbot-lock-timeout", type=float, default=0.0, help="seconds to wait for the session lock (0 = fail at once)")
    parser.add_argument("--sub-models", nargs="*", default=["gpt-6-luna", "gpt-5.6-luna", "glm-5-turbo"],
                        help="one model per Codex/GPT sub-credential to probe through CLIProxyAPI")
    parser.add_argument("--cliproxy-url", default=CLIPROXY_URL)
    parser.add_argument("--out", type=Path, help="also write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run(args)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    counts = report["counts"]
    ok = counts["bots_valid"] == counts["bots_total"] and counts["bots_total"] > 0 and (args.skip_userbot or counts["userbot_valid"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
