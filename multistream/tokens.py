"""Access to the Twitch and YouTube accounts after "Log in", shared by the chat
(Twitch viewers) and Stream info (title, category...).

What's saved (settings tokens, per destination) is the login's refresh token;
access tokens are made from it when needed and kept in memory. Twitch hands
out a new refresh token each time one is used, so every use goes through
here, one at a time, and the new one is saved straight away.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import oauth_ids
from .log import log


class NeedsLogin(Exception):
    pass


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(url, body, timeout=15) as r:
        return json.loads(r.read())


class Tokens:
    def __init__(self, settings):
        self.settings = settings
        self._cache = {}               # destination id -> (access token, good until)
        self._lock = threading.Lock()

    def saved(self, dest_id):
        """The saved login data: a dict (YouTube) or {"refresh": ...} (Twitch)."""
        raw = self.settings.token(dest_id)
        if not raw:
            return {}
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return {"refresh": raw}

    def update_saved(self, dest_id, **fields):
        data = self.saved(dest_id)
        data.update(fields)
        dest = next((d for d in self.settings.destinations() if d["id"] == dest_id), {})
        # Twitch's is kept as the bare refresh token (as the login saves it).
        if dest.get("platform") == "twitch" and set(data) == {"refresh"}:
            self.settings.set_token(dest_id, data["refresh"])
        else:
            self.settings.set_token(dest_id, json.dumps(data))

    def forget(self, dest_id):
        """The access token was refused: make a new one next time."""
        with self._lock:
            self._cache.pop(dest_id, None)

    def access(self, dest_id, platform):
        with self._lock:
            token, until = self._cache.get(dest_id, (None, 0))
            if token and time.time() < until - 60:
                return token
            refresh = self.saved(dest_id).get("refresh")
            if not refresh:
                raise NeedsLogin(f"Log in with {'Twitch' if platform == 'twitch' else 'YouTube'} first")
            try:
                if platform == "twitch":
                    got = _post_form("https://id.twitch.tv/oauth2/token", {
                        "grant_type": "refresh_token", "refresh_token": refresh,
                        "client_id": oauth_ids.TWITCH_CLIENT_ID})
                else:
                    got = _post_form("https://oauth2.googleapis.com/token", {
                        "grant_type": "refresh_token", "refresh_token": refresh,
                        "client_id": oauth_ids.GOOGLE_CLIENT_ID, "client_secret": oauth_ids.GOOGLE_CLIENT_SECRET})
            except urllib.error.HTTPError as e:
                if e.code in (400, 401):
                    self.update_saved(dest_id, refresh="")
                    name = "Twitch" if platform == "twitch" else "YouTube"
                    log(f"{name} login expired; log in again", "warn")
                    raise NeedsLogin(f"The {name} login has expired: log in again") from None
                raise
            if got.get("refresh_token") and got["refresh_token"] != refresh:
                self.update_saved(dest_id, refresh=got["refresh_token"])
            token = got["access_token"]
            self._cache[dest_id] = (token, time.time() + int(got.get("expires_in") or 3600))
            return token
