"""Log in to a platform to fetch its stream key, instead of pasting it.

    Twitch   the device login: the app shows a code, Twitch's page (opened in
             the browser) asks for it, then the app reads the channel's key.
    YouTube  Google's sign-in in the browser, back to this PC (127.0.0.1),
             then the app reads the channel's reusable stream key (making one
             called "SCC Multistream" if there isn't one).

Only the server and stream key are kept (in config.json, like a pasted key);
no login is stored. Logging in again is only needed if the key changes.

Each needs an app registered with the platform: its IDs go in oauth_ids.py.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import oauth_ids
from .log import log

TWITCH_SERVER = "rtmp://live.twitch.tv/app"
GRAPH = "https://graph.facebook.com/v21.0"
# Stream key, plus changing the title/category/tags (Stream info).
TWITCH_SCOPES = "channel:read:stream_key channel:manage:broadcast user:read:email"
FACEBOOK_SCOPES = "pages_show_list,pages_read_engagement,pages_read_user_content"
YOUTUBE_STREAM_TITLE = "SCC Multistream"


def _request(url, data=None, headers=None, method=None, tries=3):
    """JSON from an API call. Server errors (5xx) are retried a couple of times."""
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    for attempt in range(tries):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())
            except ValueError:
                detail = {}
            if e.code >= 500 and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            err = detail.get("error")
            message = detail.get("message") or (err.get("message") if isinstance(err, dict) else err)
            reasons = [x.get("reason") for x in (err.get("errors") or [])] if isinstance(err, dict) else []
            raise LoginError(message or f"HTTP {e.code}", detail, e.code, reasons[0] if reasons else None) from None


class LoginError(Exception):
    def __init__(self, message, detail=None, code=None, reason=None):
        super().__init__(message)
        self.detail, self.code, self.reason = detail or {}, code, reason


def _step(what, call, hint=None):
    """Run one API call; a failure says which step it was (and what to do)."""
    try:
        return call()
    except LoginError as e:
        log(f"{what} failed: HTTP {e.code} {e.reason or ''} {json.dumps(e.detail)[:500]}", "warn")
        extra = f" ({e.reason})" if e.reason else ""
        raise LoginError(f"{what}: {e}{extra}" + (f" {hint}" if hint else ""), e.detail, e.code, e.reason) from None


def available():
    return {"twitch": bool(oauth_ids.TWITCH_CLIENT_ID),
            "youtube": bool(oauth_ids.GOOGLE_CLIENT_ID and oauth_ids.GOOGLE_CLIENT_SECRET),
            "facebook": bool(oauth_ids.FACEBOOK_APP_ID and oauth_ids.FACEBOOK_CLIENT_TOKEN)}


class Login:
    """One login in progress, shown in the control panel."""

    def __init__(self, platform, on_key):
        self.platform = platform
        self.on_key = on_key          # (server, key, account name, extras) -> None
        self.status = "starting"      # starting, waiting (for the browser), done, error
        self.message = ""
        self.code = None              # Twitch: the code to type in
        self.url = None               # the page to log in on
        self.account = None
        self.cancelled = False

    def info(self):
        return {"platform": self.platform, "status": self.status, "message": self.message,
                "code": self.code, "url": self.url, "account": self.account}

    def run(self):
        try:
            server, key, self.account, extras = {"twitch": self._twitch, "youtube": self._youtube,
                                                 "facebook": self._facebook}[self.platform]()
            if self.cancelled:
                return
            self.on_key(server, key, self.account, extras)
            if key is None:   # Facebook: chat and viewers only
                self.status, self.message = "done", f"Connected to {self.account} for chat and viewers"
            else:
                self.status, self.message = "done", f"Logged in as {self.account}; stream key saved"
            log(f"{self.platform}: logged in as {self.account}")
        except Exception as e:
            if not self.cancelled:
                self.status, self.message = "error", str(e)
                log(f"{self.platform} login failed: {e}", "warn")

    # --- Twitch: device code ------------------------------------------------

    def _twitch(self):
        cid = oauth_ids.TWITCH_CLIENT_ID
        dev = _request("https://id.twitch.tv/oauth2/device",
                       {"client_id": cid, "scopes": TWITCH_SCOPES})
        self.code, self.url = dev["user_code"], dev["verification_uri"]
        self.status, self.message = "waiting", "Enter the code on Twitch's page"
        webbrowser.open(self.url)
        deadline = time.monotonic() + int(dev.get("expires_in", 1800))
        interval = int(dev.get("interval", 5))
        token = None
        while time.monotonic() < deadline and not self.cancelled:
            time.sleep(interval)
            try:
                got = _request("https://id.twitch.tv/oauth2/token", {
                    "client_id": cid, "device_code": dev["device_code"],
                    "scopes": TWITCH_SCOPES,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
                token = got["access_token"]
                break
            except LoginError as e:
                if "authorization_pending" in str(e) or "pending" in str(e).lower():
                    continue
                if "slow_down" in str(e):
                    interval += 5
                    continue
                raise
        if not token:
            raise LoginError("The login timed out" if not self.cancelled else "Cancelled")
        headers = {"Authorization": f"Bearer {token}", "Client-Id": cid}
        user = _request("https://api.twitch.tv/helix/users", headers=headers)["data"][0]
        key = _request(f"https://api.twitch.tv/helix/streams/key?broadcaster_id={user['id']}",
                       headers=headers)["data"][0]["stream_key"]
        # The chat to follow, and a refresh token so the chat can read the viewer count.
        extras = {"chat": user["login"], "token": got.get("refresh_token") or ""}
        return TWITCH_SERVER, key, user.get("display_name") or user.get("login"), extras

    # --- Facebook: device login, for the Pages' live viewers and comments ---

    def _facebook(self):
        app_token = f"{oauth_ids.FACEBOOK_APP_ID}|{oauth_ids.FACEBOOK_CLIENT_TOKEN}"
        dev = _request(f"{GRAPH}/device/login", {"access_token": app_token, "scope": FACEBOOK_SCOPES})
        self.code, self.url = dev["user_code"], dev.get("verification_uri") or "https://www.facebook.com/device"
        self.status, self.message = "waiting", "Enter the code on Facebook's page"
        webbrowser.open(self.url)
        deadline = time.monotonic() + int(dev.get("expires_in", 420))
        interval = int(dev.get("interval", 5))
        token = None
        while time.monotonic() < deadline and not self.cancelled:
            time.sleep(interval)
            try:
                token = _request(f"{GRAPH}/device/login_status",
                                 {"access_token": app_token, "code": dev["code"]}, tries=1)["access_token"]
                break
            except LoginError as e:
                sub = ((e.detail.get("error") or {}) if isinstance(e.detail.get("error"), dict) else {}).get("error_subcode")
                if sub == 1349174:     # waiting for the user
                    continue
                if sub == 1349172:     # polling too fast
                    interval += 5
                    continue
                if sub == 1349152:
                    raise LoginError("The code expired; press Log in again") from None
                raise
        if not token:
            raise LoginError("The login timed out" if not self.cancelled else "Cancelled")
        pages = _step("Reading your Facebook Pages", lambda: _request(
            f"{GRAPH}/me/accounts?fields=id,name,access_token&limit=50&access_token={urllib.parse.quote(token)}"))
        pages = {p["id"]: {"name": p["name"], "token": p["access_token"]}
                 for p in pages.get("data") or [] if p.get("access_token")}
        if not pages:
            raise LoginError("No Facebook Pages came back. When Facebook asks, choose the Page you stream to.")
        names = ", ".join(p["name"] for p in pages.values())
        # Page tokens from this login don't expire; the chat uses them for comments and viewers.
        return None, None, names, {"chat": ",".join(pages), "token": json.dumps(pages)}

    # --- YouTube: Google sign-in back to this PC -----------------------------

    def _youtube(self):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result = {}

        class Back(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [None])[0] == state:
                    result.update({k: v[0] for k, v in q.items()})
                ok = "code" in result
                page = ("<h2>Logged in. You can close this tab and go back to SCC Multistream.</h2>" if ok
                        else "<h2>Login didn't finish. Go back to SCC Multistream and try again.</h2>")
                body = f"<!doctype html><meta charset=utf-8><body style='font:16px system-ui;background:#08120c;color:#f2f5f2;padding:40px'>{page}".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = HTTPServer(("127.0.0.1", 0), Back)
        httpd.timeout = 1
        redirect = f"http://127.0.0.1:{httpd.server_port}/"
        self.url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": oauth_ids.GOOGLE_CLIENT_ID, "redirect_uri": redirect, "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            # offline + consent: a refresh token, so Stream info can update the broadcast later
            "access_type": "offline", "prompt": "consent select_account"})
        self.status, self.message = "waiting", "Log in with Google in the browser"
        webbrowser.open(self.url)
        deadline = time.monotonic() + 600
        try:
            while not result and time.monotonic() < deadline and not self.cancelled:
                httpd.handle_request()
        finally:
            httpd.server_close()
        if "code" not in result:
            raise LoginError("Cancelled" if self.cancelled else result.get("error") or "The login timed out")
        granted = _step("Google sign-in", lambda: _request("https://oauth2.googleapis.com/token", {
            "client_id": oauth_ids.GOOGLE_CLIENT_ID, "client_secret": oauth_ids.GOOGLE_CLIENT_SECRET,
            "code": result["code"], "code_verifier": verifier, "grant_type": "authorization_code",
            "redirect_uri": redirect}, tries=1))
        token = granted["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        api = "https://www.googleapis.com/youtube/v3"
        live_hint = ("Check that live streaming is turned on for this channel: open YouTube Studio > Go live "
                     "once (the first time, YouTube can take up to 24 hours to allow it).")
        channels = _step("Reading your YouTube channel", lambda: _request(
            f"{api}/channels?part=snippet&mine=true", headers=headers)).get("items") or []
        if not channels:
            raise LoginError("That Google account has no YouTube channel. If the channel is a brand account, "
                             "pick it (not the personal account) when Google asks.")
        name, channel_id = channels[0]["snippet"]["title"], channels[0]["id"]
        streams = _step("Reading your stream keys", lambda: _request(
            f"{api}/liveStreams?part=snippet,cdn,contentDetails&mine=true&maxResults=50",
            headers=headers), live_hint).get("items") or []
        reusable = [s for s in streams if s.get("contentDetails", {}).get("isReusable")]
        stream = next((s for s in reusable if s["snippet"]["title"] == YOUTUBE_STREAM_TITLE), None) \
            or (reusable[0] if reusable else None)
        if stream is None:
            json_headers = {**headers, "Content-Type": "application/json"}
            new_stream = {"snippet": {"title": YOUTUBE_STREAM_TITLE},
                          "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
                          "contentDetails": {"isReusable": True}}
            stream = _step("Making a stream key", lambda: _request(
                f"{api}/liveStreams?part=snippet,cdn,contentDetails", method="POST",
                data=json.dumps(new_stream).encode(), headers=json_headers), live_hint)
        ing = stream["cdn"]["ingestionInfo"]
        # Stream info needs to refresh the login later and to know which stream is ours.
        saved = json.dumps({"refresh": granted.get("refresh_token") or "", "stream_id": stream["id"]})
        return ing["ingestionAddress"], ing["streamName"], name, {"chat": channel_id, "token": saved}


class Logins:
    def __init__(self, settings):
        self.settings = settings
        self.current = {}     # destination id -> Login

    def start(self, dest_id):
        dest = next((d for d in self.settings.destinations() if d["id"] == dest_id), None)
        if dest is None or not available().get(dest["platform"]):
            raise ValueError("Logging in isn't available for this platform")
        old = self.current.get(dest_id)
        if old:
            old.cancelled = True

        def save(server, key, account, extras):
            change = {"id": dest_id, "account": account, "chat": extras.get("chat", "")}
            if key is not None:
                change.update(server=server, key=key)
            self.settings.update_destination(change)
            if extras.get("token"):
                self.settings.set_token(dest_id, extras["token"])

        login = self.current[dest_id] = Login(dest["platform"], save)
        threading.Thread(target=login.run, daemon=True).start()
        return login

    def open_page(self, dest_id):
        login = self.current.get(dest_id)
        if login and login.url:
            webbrowser.open(login.url)

    def cancel(self, dest_id):
        login = self.current.pop(dest_id, None)
        if login:
            login.cancelled = True

    def info(self):
        return {k: v.info() for k, v in self.current.items()}
