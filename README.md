# SCC Multistream

Stream once from OBS and go live on YouTube, Twitch, X, TikTok and Facebook
(and any other RTMP server) at the same time, from your own PC, with no
Restream subscription.

```
OBS --> SCC Multistream (rtmp://127.0.0.1:1935/live) --+--> YouTube
                                                        +--> Twitch
                                                        +--> X
                                                        +--> TikTok (cropped to 9:16, or OBS's vertical canvas)
                                                        +--> Facebook
```

The app runs a small RTMP server ([MediaMTX](https://github.com/bluenviron/mediamtx))
that OBS streams to, and one [FFmpeg](https://ffmpeg.org) per platform that
sends the stream on **without re-encoding it**, so it takes almost no CPU.
Your upload has to carry one copy per platform (about 5 x your OBS bitrate
for all five).

## Install

Download **SCC-Multistream-Setup.exe** from the
[latest release](https://github.com/owenbparker/multistream_scc/releases/latest)
and run it. It installs to `C:\SCC Multistream` with a desktop shortcut. The
app checks for updates when it opens (don't update mid-stream: the streams
drop while it restarts).

## Use

1. In OBS, **Settings > Stream**: Service **Custom**, Server
   `rtmp://127.0.0.1:1935/live`, Stream key `main` (both shown in the app
   with Copy buttons). In **Settings > Output**, set the keyframe interval to
   2 s (every platform asks for it).
2. In the app, open **Server and key** on each platform, paste in its
   stream key (and server where there's no default), and switch it on.
3. Start streaming in OBS. Every platform that's switched on goes live; when
   you stop, they all stop. Switch a platform on or off mid-stream and only
   that one starts or stops.

### Profiles and Save

Switching a platform on or off, or changing its server or key, isn't
applied until you press **Save** (one Save for the whole panel; unsaved
cards get a dashed border, and **Discard** undoes them). Save also stores
which platforms are on in the chosen **Profile**. Picking a profile from the
dropdown switches on exactly its platforms; **New profile** saves the
current choice under a new name (e.g. "Twitch + YouTube", "Everything").

### Chat and viewers

**Chat** (top right) shows Twitch and YouTube chat in one list, with how
many are watching on each (also on each card and in total at the top). It
follows each platform that's switched on and has its **Chat channel** set
(logging in fills it in). **Open in its own window** pops it out, and
`http://127.0.0.1:8775/chat.html` works as an OBS custom browser dock.
Twitch viewer counts need "Log in with Twitch" (done once). YouTube's chat
and viewers are read the way youtube.com's own chat window reads them. X,
TikTok and Facebook don't let apps read their live chat.

A platform that drops (or refuses the key) is retried 2, 4, 8, 15, then
every 30 seconds while the others keep going; its card shows why. **Show
log** at the bottom has the details.

### Where each platform's key is

| Platform | Server | Where to find it |
| --- | --- | --- |
| YouTube | filled in | YouTube Studio > Go live > Stream > Stream key |
| Twitch | filled in | Creator Dashboard > Settings > Stream > Primary Stream key |
| X | from X | Media Studio > Producer > Sources: create a source; copy its RTMP URL and key. Needs Media Studio/Producer access on the account. |
| TikTok | from TikTok | LIVE Studio / LIVE Center > Stream key. TikTok only gives keys to accounts with LIVE access, and the key can change each time you go live. |
| Facebook | filled in (RTMPS) | Live Producer > Streaming software > Stream key (pick a persistent key) |

**Log in with Twitch / YouTube** fetches the key instead of pasting it:
Twitch shows a code to enter on its page; YouTube opens Google's sign-in
(Google warns the app isn't verified: it's only registered for this
channel's account, so press Continue) and uses the channel's reusable
stream key, making one called "SCC Multistream" if there isn't one. Only
the key is saved, so logging in again is only needed if the key changes.
X and TikTok have no way for apps to fetch keys, and stay pasted. The
login app IDs come from the repository's Actions secrets
(`TWITCH_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).

**+ Add another RTMP destination** takes any other RTMP/RTMPS server
(Kick, a second YouTube channel, ...).

### TikTok in vertical

Each destination's **Video** setting:

- **As OBS sends it**: the stream as it is (the default).
- **Cropped to vertical (9:16)**: the middle of the 16:9 picture, re-encoded
  as 720x1280 (1080x1920 from a 1440p+ canvas) at the bitrate you set. Uses
  the graphics card (NVIDIA, AMD or Intel) when there is one, else the CPU;
  the card shows which.
- **OBS vertical output**: for a layout made for vertical. Add a vertical
  canvas in OBS (the [Aitum Vertical](https://aitum.tv/products/vertical)
  plugin) and point its stream at the same server with stream key
  `vertical`. Passed on as it is, like the main stream.

### Another PC streaming in

Tick **Let other PCs on the network stream in** and the server also
listens on the network (the app shows the address, e.g.
`rtmp://192.168.1.20:1935/live`). Windows may ask to allow it through the
firewall. Off by default, so only this PC can stream in.

## Files

`C:\SCC Multistream\config.json` holds the servers and stream keys (in plain
text, on this PC only; the control panel only answers this PC). Uninstalling
removes it.

## From source

```
pip install -r requirements-windows.txt
python tools/fetch_binaries.py       # MediaMTX + FFmpeg into bin/ (--linux: MediaMTX for Linux)
python -m multistream.gui            # the desktop app
python -m multistream                # without a window: http://127.0.0.1:8775/
python -m unittest discover -s tests # includes an end-to-end test stream through the relay
```

Every push builds on GitHub Actions (Windows), runs the tests and a smoke
test of the built app (a test stream through the relay, copied and cropped)
and publishes release `v1.0.<run>`; `[no release]` in the commit message
skips publishing.

MediaMTX (MIT) and FFmpeg (GPL, gyan.dev essentials build) are bundled
unchanged; their licenses are in `_internal\bin\licenses`.
