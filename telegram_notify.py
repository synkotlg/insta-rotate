#!/usr/bin/env python3
"""Minimal Telegram notifier, standard library only. Copy this file into any project.

Configuration, shared by every project that uses the same Telegram bot:
  TELEGRAM_BOT_TOKEN  token given by @BotFather
  TELEGRAM_CHAT_ID    chat that receives the messages
Both come from environment variables or from a .env file (load_env_file);
setup_telegram.py creates the bot and writes that file.

Each project sets its own prefix (for example "[github-follow-bot] ") so the
messages of different projects can be told apart.

CLI:
  python telegram_notify.py --chat-ids             list the chats that recently wrote to the bot
  python telegram_notify.py "hello" --prefix "[x] " send a message
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegram.org"
MAX_LENGTH = 4096

log = logging.getLogger("telegram_notify")


def urllib_post(url, data, timeout):
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def load_env_file(path, environ=None):
    """Load KEY=VALUE lines of a .env file without overriding variables already set.

    Returns the names that were loaded (never the values).
    """
    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key[len("export "):].strip() if key.startswith("export ") else key
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


class TelegramNotifier:
    def __init__(self, token, chat_id, prefix="", transport=urllib_post, timeout=15):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self.transport = transport
        self.timeout = timeout

    @classmethod
    def from_env(cls, prefix="", env=None, **kwargs):
        """Notifier built from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, or None if not configured."""
        env = os.environ if env is None else env
        token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            return cls(token, chat_id, prefix, **kwargs)
        if token or chat_id:
            log.warning("Telegram disabled: set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return None

    def _call(self, method, params):
        url = f"{API}/bot{self.token}/{method}"
        status, body = self.transport(url, urllib.parse.urlencode(params).encode(), self.timeout)
        payload = json.loads(body or b"{}")
        if status != 200 or not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed ({status}): {payload.get('description', '')}")
        return payload["result"]

    def _redact(self, err):
        return str(err).replace(self.token, "***")

    def send(self, text):
        """Send a message. Never raises: a failed notification must not break the caller."""
        message = (self.prefix + text)[:MAX_LENGTH]
        try:
            self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            })
            return True
        except Exception as err:
            log.warning("Telegram notification not sent: %s", self._redact(err))
            return False

    def me(self):
        """The bot's own account (id, username...). Raises if the token is wrong."""
        return self._call("getMe", {})

    def chats(self):
        """chat id -> name, for every chat found in the bot's recent updates."""
        found = {}
        for update in self._call("getUpdates", {}):
            for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    full_name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                    found[chat["id"]] = chat.get("title") or full_name or chat.get("username", "")
        return found


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send a Telegram message, or find your chat id.")
    parser.add_argument("message", nargs="?", help="text to send")
    parser.add_argument("--prefix", default="", help="text put before the message")
    parser.add_argument("--chat-ids", action="store_true",
                        help="list the chats that recently wrote to the bot")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env_file(".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN (token given by @BotFather)", file=sys.stderr)
        return 2

    if args.chat_ids:
        notifier = TelegramNotifier(token, None)
        try:
            chats = notifier.chats()
        except Exception as err:
            print(notifier._redact(err), file=sys.stderr)
            return 1
        if not chats:
            print("No chat found: send any message to your bot in Telegram, then run this again.")
            return 1
        for chat_id, name in chats.items():
            print(f"{chat_id}\t{name}")
        return 0

    if not args.message:
        parser.error("a message is required (or use --chat-ids)")
    notifier = TelegramNotifier.from_env(args.prefix)
    if notifier is None:
        print("Set TELEGRAM_CHAT_ID (find it with --chat-ids)", file=sys.stderr)
        return 2
    return 0 if notifier.send(args.message) else 1


if __name__ == "__main__":
    sys.exit(main())
