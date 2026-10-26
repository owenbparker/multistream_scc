"""Stream info: the title, category, tags, thumbnail... on Twitch and YouTube,
set from the app. Each profile keeps its own; it's sent to the platforms when
it's saved, when its profile is picked, and (YouTube) after each stream, so
the next one is ready.

    Twitch   the channel's title, category, tags, language and branded-content
             flag (Twitch's API; needs "Log in with Twitch").
    YouTube  a broadcast made by the app and tied to the stream key, so it's
             the one that goes live when OBS starts: title, description,
             category, privacy, made-for-kids, latency, DVR and thumbnail. If
             the stream is already live, that broadcast is changed instead.

Not possible through either API: Twitch's go-live notification text (set it
once in Twitch's Creator Dashboard), Twitch's thumbnail (Twitch makes it from
the stream) and YouTube's "game" field; YouTube notifies subscribers itself
when a public stream starts.
"""

import copy
import datetime
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import oauth_ids
from .config import DATA_DIR
from .log import log
from .tokens import NeedsLogin

THUMB_DIR = os.path.join(DATA_DIR, "thumbnails")
YT = "https://www.googleapis.com/youtube/v3"
HELIX = "https://api.twitch.tv/helix"

DEFAULT_INFO = {
    "title": "",
    "twitch": {"category_id": "", "category": "", "tags": "", "language": "en", "branded": False},
    "youtube": {"description": "", "category": "20", "privacy": "public", "kids": False,
                "latency": "normal", "dvr": True, "thumbnail": ""},
}


def merged(info):
    """A profile's saved info over the defaults (so older profiles get every field)."""
    out = copy.deepcopy(DEFAULT_INFO)
    for k, v in (info or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def clean_tags(text):
    """Twitch tags: up to 10, letters and numbers only, 25 characters each."""
    tags = []
    for t in (text or "").replace("#", "").split(","):
        t = "".join(ch for ch in t.strip() if ch.isalnum())[:25]
        if t and t.lower() not in (x.lower() for x in tags):
            tags.append(t)
    return tags[:10]


class ApiError(Exception):
    pass


def _call(url, token, method="GET", body=None, headers=None, raw=None):
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
        except ValueError:
            detail = {}
        err = detail.get("error")
        msg = detail.get("message") or (err.get("message") if isinstance(err, dict) else err) or f"HTTP {e.code}"
        if e.code == 401:
            raise PermissionError(msg) from None
        raise ApiError(msg) from None


def _with_token(tokens, dest_id, platform, fn):
    """fn(access token), retried once with a fresh token if it was refused."""
    for attempt in (1, 2):
        token = tokens.access(dest_id, platform)
        try:
            return fn(token)
        except PermissionError as e:
            tokens.forget(dest_id)
            if attempt == 2:
                raise ApiError(f"{e} - try logging in again") from None


# --- Twitch ------------------------------------------------------------------

def twitch_headers():
    return {"Client-Id": oauth_ids.TWITCH_CLIENT_ID}


def twitch_categories(tokens, dest_id, query):
    def go(token):
        url = f"{HELIX}/search/categories?first=10&query={urllib.parse.quote(query)}"
        return [{"id": c["id"], "name": c["name"], "art": c.get("box_art_url", "").replace("{width}x{height}", "52x72")}
                for c in _call(url, token, headers=twitch_headers()).get("data") or []]
    return _with_token(tokens, dest_id, "twitch", go)


def push_twitch(tokens, dest, info):
    t, title = info["twitch"], info["title"].strip()

    def go(token):
        me = _call(f"{HELIX}/users", token, headers=twitch_headers())["data"][0]
        body = {"broadcaster_language": t.get("language") or "en", "tags": clean_tags(t.get("tags")),
                "is_branded_content": bool(t.get("branded"))}
        if title:
            body["title"] = title[:140]
        if t.get("category_id"):
            body["game_id"] = t["category_id"]
        _call(f"{HELIX}/channels?broadcaster_id={me['id']}", token, "PATCH", body, headers=twitch_headers())
        return "Title, category and tags set on Twitch"
    return _with_token(tokens, dest["id"], "twitch", go)


# --- YouTube -----------------------------------------------------------------

def _now_plus(seconds):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def push_youtube(tokens, dest, info):
    y, title = info["youtube"], (info["title"].strip() or "Live")[:100]
    saved = tokens.saved(dest["id"])
    stream_id = saved.get("stream_id")
    if not stream_id:
        raise NeedsLogin("Log in with YouTube again (Stream info needs the newer login)")

    def go(token):
        status = {"privacyStatus": y.get("privacy") or "public", "selfDeclaredMadeForKids": bool(y.get("kids"))}
        active = _call(f"{YT}/liveBroadcasts?part=id,snippet,contentDetails&broadcastStatus=active"
                       f"&broadcastType=all&maxResults=10", token).get("items") or []
        live = next((b for b in active if b["contentDetails"].get("boundStreamId") == stream_id), None)
        if live:
            video_id, done = live["id"], "Live stream updated on YouTube"
        else:
            # The one made last time, if it hasn't been used: replace it.
            old = saved.get("broadcast")
            if old:
                upcoming = _call(f"{YT}/liveBroadcasts?part=id&broadcastStatus=upcoming&broadcastType=all"
                                 f"&maxResults=50", token).get("items") or []
                if any(b["id"] == old for b in upcoming):
                    _call(f"{YT}/liveBroadcasts?id={old}", token, "DELETE")
            made = _call(f"{YT}/liveBroadcasts?part=id,snippet,status,contentDetails", token, "POST", {
                "snippet": {"title": title, "description": y.get("description") or "",
                            "scheduledStartTime": _now_plus(60)},
                "status": status,
                "contentDetails": {"enableAutoStart": True, "enableAutoStop": True,
                                   "latencyPreference": y.get("latency") or "normal",
                                   "enableDvr": bool(y.get("dvr", True)),
                                   "monitorStream": {"enableMonitorStream": False}}})
            video_id = made["id"]
            _call(f"{YT}/liveBroadcasts/bind?id={video_id}&part=id,contentDetails&streamId={stream_id}", token, "POST")
            tokens.update_saved(dest["id"], broadcast=video_id)
            done = "YouTube broadcast ready: it goes live when you start streaming"
        # Category, description and privacy live on the video.
        video = (_call(f"{YT}/videos?part=snippet,status&id={video_id}", token).get("items") or [None])[0]
        if video:
            snip = video["snippet"]
            snip.update(title=title, description=y.get("description") or "", categoryId=str(y.get("category") or "20"))
            vstatus = {"privacyStatus": status["privacyStatus"],
                       "selfDeclaredMadeForKids": status["selfDeclaredMadeForKids"]}
            _call(f"{YT}/videos?part=snippet,status", token, "PUT",
                  {"id": video_id, "snippet": {k: snip[k] for k in ("title", "description", "categoryId", "tags")
                                               if k in snip}, "status": vstatus})
        thumb = y.get("thumbnail")
        if thumb:
            path = os.path.join(THUMB_DIR, os.path.basename(thumb))
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    data = f.read()
                try:
                    _call(f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
                          f"&uploadType=media", token, "POST", raw=data,
                          headers={"Content-Type": mimetypes.guess_type(path)[0] or "image/jpeg"})
                except ApiError as e:
                    done += f" (thumbnail not set: {e})"
        return done
    return _with_token(tokens, dest["id"], "youtube", go)


# --- Sending it --------------------------------------------------------------

class Pusher:
    """Sends a profile's stream info to the platforms, in the background."""

    def __init__(self, settings, tokens):
        self.settings, self.tokens = settings, tokens
        self.results = {}          # platform -> {"status": working|ok|error, "message", "time"}
        self._lock = threading.Lock()

    def info(self):
        return self.results

    def push(self, reason=""):
        snap = self.settings.snapshot()
        profile = next((p for p in snap["profiles"] if p["name"] == snap["profile"]), None)
        info = merged(profile.get("info") if profile else None)
        if not info["title"].strip():
            return   # nothing set for this profile: leave the platforms as they are
        targets = [d for d in snap["destinations"]
                   if d["enabled"] and d["platform"] in ("twitch", "youtube") and self.tokens.saved(d["id"]).get("refresh")]
        for d in targets:
            self.results[d["platform"]] = {"status": "working", "message": "Updating…", "time": time.time()}
            threading.Thread(target=self._one, args=(d, info, reason), daemon=True).start()

    def _one(self, dest, info, reason):
        fn = push_twitch if dest["platform"] == "twitch" else push_youtube
        with self._lock:     # one at a time: YouTube's broadcast juggling must not overlap
            try:
                msg = fn(self.tokens, dest, info)
                self.results[dest["platform"]] = {"status": "ok", "message": msg, "time": time.time()}
                log(f"{dest['name']}: {msg}" + (f" ({reason})" if reason else ""))
            except Exception as e:
                self.results[dest["platform"]] = {"status": "error", "message": str(e), "time": time.time()}
                log(f"{dest['name']} stream info: {e}", "warn")


def save_thumbnail(data_url):
    """A picture from the panel (data: URL, JPEG or PNG, up to 2 MB) -> its file name."""
    import base64
    import hashlib
    head, _, b64 = (data_url or "").partition(",")
    ext = ".png" if "image/png" in head else ".jpg" if "image/jpeg" in head else None
    if not ext:
        raise ValueError("The thumbnail must be a JPG or PNG")
    data = base64.b64decode(b64)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("YouTube thumbnails can be at most 2 MB")
    os.makedirs(THUMB_DIR, exist_ok=True)
    name = hashlib.sha1(data).hexdigest()[:16] + ext
    with open(os.path.join(THUMB_DIR, name), "wb") as f:
        f.write(data)
    return name
