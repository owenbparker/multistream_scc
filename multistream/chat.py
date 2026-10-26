"""One chat for every platform that has one we can read, and how many are
watching on each.

    Twitch   the channel's chat over Twitch's IRC (read-only, no login
             needed); viewers from Twitch's API, using the login from
             "Log in with Twitch" (kept as a refresh token).
    YouTube  the live stream's chat and viewer count, read the way the
             youtube.com chat window reads them (no login, no API quota).
             Waits for the channel to be live, then follows that stream.

    Facebook the Page's live video comments and viewers, from Facebook's API,
             using the Page access from "Log in with Facebook".

X and TikTok have no way for an app to read their live chat.

Each platform that's switched on and has a chat channel set is followed;
the control panel (and its /chat.html page, which also works as an OBS
custom browser dock) shows the messages and viewer counts.
"""

import collections
import itertools
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import oauth_ids
from .log import log
from .tokens import NeedsLogin, Tokens

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
TWITCH_EMOTE = "https://static-cdn.jtvnw.net/emoticons/v2/{}/default/dark/1.0"
VIEWERS_EVERY = 30


class Hub:
    """The messages and viewer counts, and a follower per platform."""

    def __init__(self, settings, tokens=None):
        self.settings = settings
        self.tokens = tokens or Tokens(settings)
        self.messages = collections.deque(maxlen=400)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.followers = {}          # destination id -> TwitchChat / YouTubeChat

    def add(self, dest, author, parts, color=None, kind="chat", amount=None):
        msg = {"id": next(self._ids), "dest": dest["id"], "platform": dest["platform"], "author": author,
               "color": color, "parts": parts, "kind": kind, "amount": amount, "time": time.time()}
        with self._lock:
            self.messages.append(msg)

    def since(self, after=0):
        with self._lock:
            return [m for m in self.messages if m["id"] > after]

    def sync(self):
        """Follow each switched-on Twitch/YouTube destination that has a chat channel."""
        want = {}
        for d in self.settings.destinations():
            channel = (d.get("chat") or "").strip()
            if d["platform"] == "facebook" and not self.settings.token(d["id"]):
                channel = ""
            if d["enabled"] and channel and d["platform"] in ("twitch", "youtube", "facebook"):
                want[d["id"]] = d
        for dest_id in list(self.followers):
            f = self.followers[dest_id]
            d = want.get(dest_id)
            if d is None or d["chat"].strip() != f.channel:
                f.stop()
                del self.followers[dest_id]
        for dest_id, d in want.items():
            if dest_id not in self.followers:
                cls = {"twitch": TwitchChat, "youtube": YouTubeChat, "facebook": FacebookChat}[d["platform"]]
                f = self.followers[dest_id] = cls(self, d)
                f.start()

    def viewers(self):
        return {k: f.info() for k, f in self.followers.items()}

    def stop(self):
        for f in self.followers.values():
            f.stop()
        self.followers = {}


class Follower:
    def __init__(self, hub, dest):
        self.hub, self.dest = hub, dest
        self.channel = dest["chat"].strip()
        self.viewers = None          # None: not live (or unknown)
        self.status, self.message = "connecting", ""
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run_guarded, daemon=True).start()

    def _run_guarded(self):
        while not self._stop.is_set():
            try:
                self.run()
            except Exception as e:
                self.status, self.message = "error", str(e)
                log(f"{self.dest['name']} chat: {e}", "warn")
            self._stop.wait(10)

    def stop(self):
        self._stop.set()

    def info(self):
        return {"platform": self.dest["platform"], "channel": self.channel, "viewers": self.viewers,
                "status": self.status, "message": self.message}


# --- Twitch ------------------------------------------------------------------

def parse_irc(line):
    """'@tags :prefix COMMAND params :trailing' -> (tags, prefix, command, params, trailing)."""
    tags, prefix = {}, ""
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v.replace("\\s", " ").replace("\\:", ";").replace("\\\\", "\\")
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    line, _, trailing = line.partition(" :")
    command, *params = line.split()
    return tags, prefix, command, params, trailing


def twitch_parts(text, emotes):
    """Message text + Twitch's emotes tag ('25:0-4,12-16/1902:6-10') -> text and image parts."""
    spans = []
    for group in filter(None, (emotes or "").split("/")):
        emote_id, _, ranges = group.partition(":")
        for r in ranges.split(","):
            a, _, b = r.partition("-")
            if a.isdigit() and b.isdigit():
                spans.append((int(a), int(b), emote_id))
    parts, pos = [], 0
    for a, b, emote_id in sorted(spans):
        if a > pos:
            parts.append({"t": text[pos:a]})
        parts.append({"img": TWITCH_EMOTE.format(emote_id), "alt": text[a:b + 1]})
        pos = b + 1
    if pos < len(text):
        parts.append({"t": text[pos:]})
    return parts


class TwitchChat(Follower):
    def __init__(self, hub, dest):
        super().__init__(hub, dest)
        self.channel = self.channel.lower().lstrip("#").rsplit("/", 1)[-1]

    def start(self):
        super().start()
        threading.Thread(target=self._watch_viewers, daemon=True).start()

    def run(self):
        raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=360)
        with ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv") as s:
            send = lambda text: s.sendall((text + "\r\n").encode())
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            send("NICK justinfan%d" % (int(time.time()) % 100000))
            send(f"JOIN #{self.channel}")
            self.status, self.message = "connected", ""
            log(f"Twitch chat: following #{self.channel}")
            buf = b""
            while not self._stop.is_set():
                data = s.recv(65536)
                if not data:
                    raise ConnectionError("Twitch chat disconnected")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for line in lines:
                    self._line(line.decode("utf-8", "replace"), send)

    def _line(self, line, send):
        if line.startswith("PING"):
            send("PONG" + line[4:])
            return
        tags, prefix, command, params, text = parse_irc(line)
        if command == "PRIVMSG":
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = text[8:-1]
            name = tags.get("display-name") or prefix.split("!", 1)[0]
            bits = tags.get("bits")
            self.hub.add(self.dest, name, twitch_parts(text, tags.get("emotes")), tags.get("color") or None,
                         kind="paid" if bits else "chat", amount=f"{bits} bits" if bits else None)
        elif command == "USERNOTICE" and tags.get("system-msg"):
            self.hub.add(self.dest, "Twitch", [{"t": tags["system-msg"] + (f" - {text}" if text else "")}],
                         kind="event")
        elif command == "RECONNECT":
            raise ConnectionError("Twitch asked to reconnect")

    # Viewers come from Twitch's API, which needs the login.
    def _api(self, url):
        for attempt in (1, 2):
            try:
                token = self.hub.tokens.access(self.dest["id"], "twitch")
            except NeedsLogin as e:
                self.message = f"{e} to see viewers"
                return None
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                       "Client-Id": oauth_ids.TWITCH_CLIENT_ID})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self.hub.tokens.forget(self.dest["id"])
                    continue
                raise

    def _watch_viewers(self):
        while not self._stop.is_set():
            try:
                data = self._api(f"https://api.twitch.tv/helix/streams?user_login={urllib.parse.quote(self.channel)}")
                if data is not None:
                    live = (data.get("data") or [None])[0]
                    self.viewers = live["viewer_count"] if live else None
            except Exception as e:
                log(f"Twitch viewers: {e}", "warn")
            self._stop.wait(VIEWERS_EVERY)


# --- YouTube -----------------------------------------------------------------

def youtube_live_url(channel):
    """'UC…' channel id, '@handle', or a youtube.com link -> the channel's /live page."""
    c = channel.strip()
    m = re.search(r"youtube\.com/(channel/UC[\w-]+|@[\w.-]+|c/[\w.-]+)", c)
    if m:
        return f"https://www.youtube.com/{m.group(1)}/live"
    if re.fullmatch(r"UC[\w-]{20,}", c):
        return f"https://www.youtube.com/channel/{c}/live"
    return f"https://www.youtube.com/@{c.lstrip('@')}/live"


def find_json(html, name):
    """The JSON object assigned to `name` in a YouTube page (ytInitialData etc.)."""
    m = re.search(r"(?:var |window\[\"|)" + re.escape(name) + r"\"?\]?\s*=\s*", html)
    if not m:
        return None
    return json.JSONDecoder().raw_decode(html[m.end():])[0]


def first_continuation(conts):
    for c in conts or []:
        for kind in ("invalidationContinuationData", "timedContinuationData",
                     "reloadContinuationData", "liveChatReplayContinuationData"):
            if kind in c:
                return c[kind].get("continuation"), int(c[kind].get("timeoutMs") or 3000)
    return None, 3000


def youtube_parts(runs):
    parts = []
    for r in runs or []:
        if "text" in r:
            parts.append({"t": r["text"]})
        elif "emoji" in r:
            e = r["emoji"]
            thumbs = (e.get("image") or {}).get("thumbnails") or []
            alt = (e.get("shortcuts") or [e.get("emojiId", "")])[0]
            if thumbs and e.get("isCustomEmoji"):
                parts.append({"img": thumbs[-1]["url"], "alt": alt})
            else:
                parts.append({"t": e.get("emojiId") if not e.get("isCustomEmoji") else alt})
    return parts


def youtube_messages(actions):
    """get_live_chat actions -> (author, parts, kind, amount) for each new message."""
    out = []
    for a in actions or []:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        for kind, key in (("chat", "liveChatTextMessageRenderer"), ("paid", "liveChatPaidMessageRenderer"),
                          ("event", "liveChatMembershipItemRenderer")):
            r = item.get(key)
            if not r:
                continue
            author = (r.get("authorName") or {}).get("simpleText") or "YouTube"
            parts = youtube_parts((r.get("message") or {}).get("runs"))
            if kind == "event" and not parts:
                parts = youtube_parts((r.get("headerSubtext") or {}).get("runs"))
            amount = (r.get("purchaseAmountText") or {}).get("simpleText")
            out.append((author, parts, kind, amount))
    return out


def youtube_viewers(actions):
    for a in actions or []:
        v = (a.get("updateViewershipAction") or {}).get("viewCount", {}).get("videoViewCountRenderer")
        if v:
            if str(v.get("originalViewCount", "")).isdigit():
                return int(v["originalViewCount"])
            text = (v.get("viewCount") or {}).get("simpleText") or \
                "".join(r.get("text", "") for r in (v.get("viewCount") or {}).get("runs", []))
            digits = re.sub(r"\D", "", text.split(" ")[0])
            if digits:
                return int(digits)
    return None


class YouTubeChat(Follower):
    def _get(self, url, body=None):
        headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
                   "Cookie": "CONSENT=YES+1; SOCS=CAI"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=15) as r:
            data = r.read().decode("utf-8", "replace")
        return json.loads(data) if body is not None else data

    def live_video(self):
        """The video id the channel is live on now, or None."""
        html = self._get(youtube_live_url(self.channel))
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
        if m and re.search(r'"isLive(?:Now)?":true', html):
            return m.group(1)
        return None

    def run(self):
        self.status, self.message, self.viewers = "waiting", "Waiting for the channel to go live", None
        video = None
        while not self._stop.is_set() and not video:
            video = self.live_video()
            if not video:
                self._stop.wait(20)
        if not video:
            return
        page = self._get(f"https://www.youtube.com/live_chat?is_popout=1&v={video}")
        ver = (re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page) or [None, "2.20241001.00.00"])[1]
        key = (re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page) or [None, ""])[1]
        data = find_json(page, "ytInitialData") or {}
        renderer = (data.get("contents") or {}).get("liveChatRenderer") or {}
        cont, wait = first_continuation(renderer.get("continuations"))
        if not cont:
            raise ValueError("This live stream's chat is turned off")
        context = {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}}
        q = f"?key={key}&prettyPrint=false" if key else "?prettyPrint=false"
        self.status, self.message = "connected", ""
        log(f"YouTube chat: following live video {video}")
        next_viewers = 0
        while not self._stop.is_set():
            if time.monotonic() >= next_viewers:
                next_viewers = time.monotonic() + VIEWERS_EVERY
                try:
                    meta = self._get(f"https://www.youtube.com/youtubei/v1/updated_metadata{q}",
                                     {"context": context, "videoId": video})
                    self.viewers = youtube_viewers(meta.get("actions"))
                except Exception as e:
                    log(f"YouTube viewers: {e}", "warn")
            self._stop.wait(min(max(wait, 1000), 8000) / 1000)
            got = self._get(f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat{q}",
                            {"context": context, "continuation": cont})
            chat = (got.get("continuationContents") or {}).get("liveChatContinuation")
            if not chat:
                self.message = "The live stream ended"
                return     # back to waiting for the next one
            for author, parts, kind, amount in youtube_messages(chat.get("actions")):
                self.hub.add(self.dest, author, parts, kind=kind, amount=amount)
            cont, wait = first_continuation(chat.get("continuations"))
            if not cont:
                return


# --- Facebook ----------------------------------------------------------------

GRAPH = "https://graph.facebook.com/v21.0"


class FacebookChat(Follower):
    """Polls each logged-in Page for a live video, then its comments and viewers."""

    def _get(self, path, token, **params):
        params["access_token"] = token
        url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error") or {}
            except ValueError:
                err = {}
            if err.get("code") == 190:    # the login no longer works
                raise PermissionError("Log in with Facebook again") from None
            raise RuntimeError(err.get("message") or f"HTTP {e.code}") from None

    def run(self):
        pages = json.loads(self.hub.settings.token(self.dest["id"]) or "{}")
        seen = set()
        first = {}                       # live video id -> True once its old comments are skipped
        self.status, self.message = "waiting", "Waiting for the Page to go live"
        last_views = 0
        while not self._stop.is_set():
            live, views = [], 0
            for page_id, page in pages.items():
                got = self._get(f"{page_id}/live_videos", page["token"], broadcast_status='["LIVE"]',
                                fields="id,live_views,status")
                for v in got.get("data") or []:
                    live.append((v["id"], page["token"]))
                    views += int(v.get("live_views") or 0)
            if live:
                self.status, self.message = "connected", ""
                if time.monotonic() - last_views >= 5:
                    self.viewers, last_views = views, time.monotonic()
                for video_id, token in live:
                    got = self._get(f"{video_id}/comments", token, order="reverse_chronological",
                                    live_filter="no_filter", limit=50, fields="id,from{name},message")
                    fresh = [c for c in got.get("data") or [] if c["id"] not in seen]
                    seen.update(c["id"] for c in fresh)
                    if video_id not in first:    # joining mid-stream: don't replay the backlog
                        first[video_id] = True
                        continue
                    for c in reversed(fresh):
                        name = (c.get("from") or {}).get("name") or "Facebook user"
                        self.hub.add(self.dest, name, [{"t": c.get("message") or ""}])
                self._stop.wait(3)
            else:
                self.viewers = None
                self.status, self.message = "waiting", "Waiting for the Page to go live"
                self._stop.wait(15)
