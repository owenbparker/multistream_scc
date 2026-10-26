import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multistream import procs  # noqa: E402
from multistream.config import tool  # noqa: E402
from multistream.relay import (CROP_FILTER, Ingest, Output, Relay, explain, ffmpeg_command,  # noqa: E402
                               mediamtx_config, redact)
from multistream.settings import CROP, VERTICAL, Settings, clean_destination, new_destination, target_url  # noqa: E402


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_defaults_have_every_platform_off(self):
        s = Settings(self.path)
        self.assertEqual([d["id"] for d in s.destinations()], ["youtube", "twitch", "x", "tiktok", "facebook"])
        self.assertFalse(any(d["enabled"] for d in s.destinations()))
        self.assertEqual(next(d for d in s.destinations() if d["id"] == "tiktok")["video"], CROP)

    def test_saves_and_loads(self):
        s = Settings(self.path)
        s.update_destination({"id": "twitch", "key": " live_123 ", "enabled": True})
        s.update_destination({"platform": "custom", "name": "Kick", "server": "rtmps://kick.example/app"})
        again = Settings(self.path)
        twitch = next(d for d in again.destinations() if d["id"] == "twitch")
        self.assertEqual((twitch["key"], twitch["enabled"]), ("live_123", True))
        self.assertEqual(again.destinations()[-1]["name"], "Kick")
        self.assertTrue(again.destinations()[-1]["id"].startswith("rtmp-"))
        again.remove_destination(again.destinations()[-1]["id"])
        self.assertEqual(len(Settings(self.path).destinations()), 5)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            clean_destination({"platform": "custom", "server": "http://example.com"})
        with self.assertRaises(ValueError):
            clean_destination({"platform": "custom", "video": "sideways"})
        self.assertEqual(clean_destination({"platform": "custom", "bitrate": 99999})["bitrate"], 20000)

    def test_pasting_a_key_forgets_the_logged_in_account(self):
        s = Settings(self.path)
        s.update_destination({"id": "twitch", "key": "a", "account": "SCCSaints"})
        self.assertEqual(s.destinations()[1]["account"], "SCCSaints")
        s.update_destination({"id": "twitch", "key": "a", "enabled": True})   # same key: kept
        self.assertEqual(s.destinations()[1]["account"], "SCCSaints")
        s.update_destination({"id": "twitch", "key": "b"})
        self.assertEqual(s.destinations()[1]["account"], "")

    def test_login_needs_the_app_ids(self):
        from multistream import accounts, oauth_ids
        saved = (oauth_ids.TWITCH_CLIENT_ID, oauth_ids.GOOGLE_CLIENT_ID, oauth_ids.FACEBOOK_APP_ID)
        try:
            oauth_ids.TWITCH_CLIENT_ID, oauth_ids.GOOGLE_CLIENT_ID, oauth_ids.FACEBOOK_APP_ID = "", "", ""
            self.assertEqual(accounts.available(), {"twitch": False, "youtube": False, "facebook": False})
            with self.assertRaises(ValueError):
                accounts.Logins(Settings(self.path)).start("twitch")
        finally:
            oauth_ids.TWITCH_CLIENT_ID, oauth_ids.GOOGLE_CLIENT_ID, oauth_ids.FACEBOOK_APP_ID = saved

    def test_profiles(self):
        s = Settings(self.path)
        s.save_all([{"id": "youtube", "enabled": True, "key": "yt"}, {"id": "twitch", "enabled": True}])
        self.assertEqual(s.snapshot()["profiles"], [{"name": "Main", "platforms": ["youtube", "twitch"]}])
        s.add_profile("Twitch only", ["twitch"])
        s.select_profile("Twitch only")
        self.assertEqual([d["id"] for d in s.destinations() if d["enabled"]], ["twitch"])
        s.save_all([{"id": "tiktok", "enabled": True}])      # saved into the chosen profile
        again = Settings(self.path)
        self.assertEqual(again.snapshot()["profile"], "Twitch only")
        self.assertEqual(again.snapshot()["profiles"][1]["platforms"], ["twitch", "tiktok"])
        again.select_profile("Main")
        self.assertEqual([d["id"] for d in again.destinations() if d["enabled"]], ["youtube", "twitch"])
        with self.assertRaises(ValueError):
            again.add_profile("main", [])
        with self.assertRaises(ValueError):     # a bad change saves nothing
            again.save_all([{"id": "x", "enabled": True}, {"id": "twitch", "server": "http://nope"}])
        self.assertFalse(next(d for d in again.destinations() if d["id"] == "x")["enabled"])
        again.delete_profile("Main")
        self.assertEqual(again.snapshot()["profile"], "Twitch only")
        with self.assertRaises(ValueError):
            again.delete_profile("Twitch only")

    def test_old_settings_get_a_profile_of_what_was_on(self):
        with open(self.path, "w") as f:
            json.dump({"destinations": [new_destination("twitch", enabled=True), new_destination("youtube")]}, f)
        self.assertEqual(Settings(self.path).snapshot()["profiles"], [{"name": "Main", "platforms": ["twitch"]}])

    def test_broken_file_falls_back_to_defaults(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        self.assertEqual(len(Settings(self.path).destinations()), 5)

    def test_target_url(self):
        self.assertEqual(target_url({"server": "rtmp://a.rtmp.youtube.com/live2/", "key": "abc"}),
                         "rtmp://a.rtmp.youtube.com/live2/abc")
        self.assertEqual(target_url({"server": "rtmps://live-api-s.facebook.com:443/rtmp/", "key": "FB-1"}),
                         "rtmps://live-api-s.facebook.com:443/rtmp/FB-1")
        self.assertEqual(target_url({"server": "rtmp://x/full/url", "key": ""}), "rtmp://x/full/url")


class CommandTest(unittest.TestCase):
    def test_copy_does_not_reencode(self):
        d = new_destination("twitch", key="k")
        args = ffmpeg_command(d, "rtmp://127.0.0.1:1935/live/main", ffmpeg="ffmpeg")
        self.assertIn("copy", args)
        self.assertNotIn("-vf", args)
        self.assertEqual(args[-1], "rtmp://live.twitch.tv/app/k")
        self.assertEqual(args[args.index("-i") + 1], "rtmp://127.0.0.1:1935/live/main")

    def test_crop_reencodes_with_the_chosen_encoder(self):
        d = new_destination("tiktok", server="rtmp://push.example/game", key="k", bitrate=4000)
        args = ffmpeg_command(d, "rtmp://127.0.0.1:1935/live/main", encoder="h264_nvenc", ffmpeg="ffmpeg")
        self.assertEqual(args[args.index("-vf") + 1], CROP_FILTER)
        self.assertEqual(args[args.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(args[args.index("-b:v") + 1], "4000k")

    def test_mediamtx_config(self):
        self.assertIn("rtmpAddress: 127.0.0.1:1935", mediamtx_config(False))
        self.assertIn("rtmpAddress: :1935", mediamtx_config(True))
        self.assertIn("apiAddress: 127.0.0.1:", mediamtx_config(True))

    def test_keys_kept_out_of_messages(self):
        self.assertEqual(redact("rtmp://x/app/live_abc123: I/O error", {"key": "live_abc123"}), "rtmp://x/app/••••: I/O error")

    def test_explain(self):
        self.assertIn("server address", explain(["[tcp @ 0] Connection to tcp://x:1935 failed: Connection refused"], 1))
        self.assertIn("stream key", explain(["rtmp://x/app/••••: Input/output error", "Error opening output files: Input/output error"], 1))
        self.assertEqual(explain([], 3), "FFmpeg stopped (exit code 3)")


class FakeIngest:
    def __init__(self):
        self.paths, self.rates, self.lan, self.rtmp_port, self.error = {}, {}, False, 1935, None

    def source(self, vertical=False):
        return Ingest.source(self, vertical)

    def url(self, path):
        return "rtmp://127.0.0.1:1935/" + path

    def alive(self):
        return True


class WatcherTest(unittest.TestCase):
    """The watcher's decisions, without starting any programs."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.dir, "config.json"))
        self.ingest = FakeIngest()
        self.relay = Relay(self.settings, self.ingest)
        self.relay.encoder = "libx264"
        self.started = []
        self.real_start = Output.start
        Output.start = lambda out, args: (self.started.append(out.dest["id"]), setattr(out, "status", "starting"))

    def tearDown(self):
        Output.start = self.real_start
        shutil.rmtree(self.dir, ignore_errors=True)

    def status(self):
        return {o["id"]: o["status"] for o in self.relay.state()["outputs"]}

    def test_waits_for_obs_then_starts_what_is_on(self):
        self.settings.update_destination({"id": "youtube", "key": "k", "enabled": True})
        self.settings.update_destination({"id": "x", "enabled": True})    # no server yet
        self.relay.tick()
        self.assertEqual(self.status()["youtube"], "waiting")
        self.assertEqual(self.status()["x"], "setup")
        self.assertEqual(self.status()["twitch"], "off")
        self.ingest.paths = {"live/main": {"ready": True}}
        self.relay.tick()
        self.assertEqual(self.started, ["youtube"])

    def test_vertical_needs_the_vertical_feed(self):
        self.settings.update_destination({"id": "tiktok", "server": "rtmp://t/game", "key": "k", "enabled": True, "video": VERTICAL})
        self.ingest.paths = {"live/main": {"ready": True}}
        self.relay.tick()
        self.assertEqual(self.status()["tiktok"], "waiting")
        self.ingest.paths["live/vertical"] = {"ready": True}
        self.relay.tick()
        self.assertEqual(self.started, ["tiktok"])
        self.assertEqual(self.ingest.source(), "live/main")
        self.assertEqual(self.ingest.source(vertical=True), "live/vertical")


class ChatTest(unittest.TestCase):
    def test_twitch_message_with_emotes(self):
        from multistream.chat import parse_irc, twitch_parts
        line = ("@badge-info=;color=#1E90FF;display-name=Saints\\sFan;emotes=25:0-4,12-16/1902:6-10;id=x "
                ":saintsfan!saintsfan@saintsfan.tmi.twitch.tv PRIVMSG #scc :Kappa Keepo Kappa gg")
        tags, prefix, command, params, text = parse_irc(line)
        self.assertEqual((command, params, text), ("PRIVMSG", ["#scc"], "Kappa Keepo Kappa gg"))
        self.assertEqual(tags["color"], "#1E90FF")
        parts = twitch_parts(text, tags["emotes"])
        self.assertEqual([p.get("alt") or p["t"] for p in parts], ["Kappa", " ", "Keepo", " ", "Kappa", " gg"])
        self.assertIn("/25/", parts[0]["img"])

    def test_twitch_hub_receives_messages(self):
        from multistream.chat import Hub, TwitchChat
        hub = Hub(Settings(os.path.join(tempfile.mkdtemp(), "c.json")))
        tc = TwitchChat(hub, new_destination("twitch", chat="SCC"))
        self.assertEqual(tc.channel, "scc")
        sent = []
        tc._line("PING :tmi.twitch.tv", sent.append)
        tc._line("@display-name=Fan;bits=100;emotes= :fan!fan@x PRIVMSG #scc :cheer100 hype", sent.append)
        self.assertEqual(sent, ["PONG :tmi.twitch.tv"])
        m = hub.since()[0]
        self.assertEqual((m["author"], m["kind"], m["amount"], m["platform"]), ("Fan", "paid", "100 bits", "twitch"))

    def test_youtube_pieces(self):
        from multistream.chat import (find_json, first_continuation, youtube_live_url, youtube_messages,
                                      youtube_viewers)
        self.assertEqual(youtube_live_url("@SCCSaints"), "https://www.youtube.com/@SCCSaints/live")
        self.assertEqual(youtube_live_url("UCabcdefghijklmnopqrstuv"),
                         "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/live")
        self.assertEqual(youtube_live_url("https://www.youtube.com/@scc/videos"), "https://www.youtube.com/@scc/live")
        page = 'x<script>window["ytInitialData"] = {"contents": {"liveChatRenderer": {"continuations": [{"invalidationContinuationData": {"continuation": "C1", "timeoutMs": 5000}}]}}};</script>'
        data = find_json(page, "ytInitialData")
        self.assertEqual(first_continuation(data["contents"]["liveChatRenderer"]["continuations"]), ("C1", 5000))
        actions = [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "authorName": {"simpleText": "Viewer"},
                "message": {"runs": [{"text": "hi "}, {"emoji": {"emojiId": "😀", "shortcuts": [":grin:"]}},
                                     {"emoji": {"emojiId": "UC/x", "isCustomEmoji": True, "shortcuts": [":scc:"],
                                                "image": {"thumbnails": [{"url": "https://yt3/x.png"}]}}}]}}}}},
            {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
                "authorName": {"simpleText": "Big Fan"}, "purchaseAmountText": {"simpleText": "CA$5.00"},
                "message": {"runs": [{"text": "go saints"}]}}}}},
            {"markChatItemAsDeletedAction": {}},
        ]
        msgs = youtube_messages(actions)
        self.assertEqual(msgs[0][0], "Viewer")
        self.assertEqual(msgs[0][1], [{"t": "hi "}, {"t": "😀"}, {"img": "https://yt3/x.png", "alt": ":scc:"}])
        self.assertEqual(msgs[1][2:], ("paid", "CA$5.00"))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(youtube_viewers([{"updateViewershipAction": {"viewCount": {"videoViewCountRenderer": {
            "viewCount": {"simpleText": "1,234 watching now"}, "originalViewCount": "1234"}}}}]), 1234)
        self.assertEqual(youtube_viewers([{"updateViewershipAction": {"viewCount": {"videoViewCountRenderer": {
            "viewCount": {"runs": [{"text": "56"}, {"text": " watching now"}]}}}}}]), 56)

    def test_facebook_comments_and_viewers(self):
        from multistream import chat
        settings = Settings(os.path.join(tempfile.mkdtemp(), "c.json"))
        settings.set_token("facebook", json.dumps({"p1": {"name": "SCC", "token": "t"}}))
        hub = chat.Hub(settings)
        fb = chat.FacebookChat(hub, new_destination("facebook", chat="p1"))
        calls = {"n": 0}
        def fake(path, token, **params):
            if path.endswith("live_videos"):
                return {"data": [{"id": "v1", "live_views": 42}]}
            calls["n"] += 1
            if calls["n"] == 1:
                return {"data": [{"id": "c0", "from": {"name": "Old"}, "message": "before we joined"}]}
            if calls["n"] == 2:
                fb.stop()
                return {"data": [{"id": "c2", "from": {"name": "B"}, "message": "two"},
                                 {"id": "c1", "message": "one"}, {"id": "c0", "message": "old"}]}
            return {"data": []}
        fb._get = fake
        fb.run()
        self.assertEqual(fb.viewers, 42)
        self.assertEqual([(m["author"], m["parts"][0]["t"]) for m in hub.since()], [("Facebook user", "one"), ("B", "two")])

    def test_hub_follows_only_switched_on_channels(self):
        from multistream import chat
        settings = Settings(os.path.join(tempfile.mkdtemp(), "c.json"))
        hub = chat.Hub(settings)
        started = []
        real = chat.Follower.start
        chat.Follower.start = lambda f: started.append(f.dest["id"])
        try:
            settings.update_destination({"id": "twitch", "chat": "scc", "enabled": False})
            settings.update_destination({"id": "youtube", "chat": "@scc", "enabled": True})
            settings.update_destination({"id": "x", "chat": "scc", "enabled": True})
            hub.sync()
            self.assertEqual(started, ["youtube"])
            settings.update_destination({"id": "youtube", "enabled": False})
            hub.sync()
            self.assertEqual(hub.followers, {})
        finally:
            chat.Follower.start = real


class StreamInfoTest(unittest.TestCase):
    def setUp(self):
        from multistream import streaminfo
        from multistream.tokens import Tokens
        self.si = streaminfo
        self.settings = Settings(os.path.join(tempfile.mkdtemp(), "c.json"))
        self.tokens = Tokens(self.settings)
        self.tokens.access = lambda dest_id, platform: "ACCESS"
        self.calls = []
        self.real_call = streaminfo._call

    def tearDown(self):
        self.si._call = self.real_call

    def fake(self, responses):
        def call(url, token, method="GET", body=None, headers=None, raw=None):
            self.calls.append((method, url.split("?")[0].rsplit("/", 2)[-2:], body))
            for match, reply in responses:
                if match in url and (method == "GET") == ("GET" in match or method == "GET"):
                    return reply(url, body) if callable(reply) else reply
            return {}
        self.si._call = call

    def test_tags_and_defaults(self):
        self.assertEqual(self.si.clean_tags("Esports, #College Esports, esports, a-b!c, " + "x" * 30),
                         ["Esports", "CollegeEsports", "abc", "x" * 25])
        info = self.si.merged({"title": "T", "youtube": {"privacy": "unlisted"}})
        self.assertEqual((info["title"], info["youtube"]["privacy"], info["youtube"]["category"]), ("T", "unlisted", "20"))

    def test_twitch(self):
        self.fake([("/users", {"data": [{"id": "77"}]})])
        info = self.si.merged({"title": "Saints vs Lambton", "twitch": {"category_id": "516575", "tags": "esports, valorant"}})
        self.si.push_twitch(self.tokens, new_destination("twitch"), info)
        method, path, body = self.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(body, {"broadcaster_language": "en", "tags": ["esports", "valorant"], "is_branded_content": False,
                                "title": "Saints vs Lambton", "game_id": "516575"})

    def test_youtube_makes_a_broadcast_for_the_next_stream(self):
        self.settings.set_token("youtube", json.dumps({"refresh": "r", "stream_id": "S1", "broadcast": "OLD"}))
        responses = [
            ("broadcastStatus=active", {"items": []}),
            ("broadcastStatus=upcoming", {"items": [{"id": "OLD"}]}),
            ("/liveBroadcasts/bind", {}),
            ("/liveBroadcasts?part=id,snippet,status,contentDetails", {"id": "NEW"}),
            ("/videos?part=snippet,status&id=", {"items": [{"id": "NEW", "snippet": {"title": "x", "categoryId": "22"}}]}),
        ]
        def call(url, token, method="GET", body=None, headers=None, raw=None):
            self.calls.append((method, url, body))
            for match, reply in responses:
                if match in url:
                    return reply
            return {}
        self.si._call = call
        info = self.si.merged({"title": "Finals", "youtube": {"privacy": "unlisted", "category": "17"}})
        msg = self.si.push_youtube(self.tokens, new_destination("youtube"), info)
        self.assertIn("ready", msg)
        methods = [(m, u.split("?")[0].split("/v3/")[-1]) for m, u, b in self.calls]
        self.assertIn(("DELETE", "liveBroadcasts"), methods)
        self.assertIn(("POST", "liveBroadcasts/bind"), methods)
        insert = next(b for m, u, b in self.calls if m == "POST" and u.split("?")[0].endswith("liveBroadcasts"))
        self.assertEqual(insert["snippet"]["title"], "Finals")
        self.assertTrue(insert["contentDetails"]["enableAutoStart"])
        put = next(b for m, u, b in self.calls if m == "PUT")
        self.assertEqual((put["snippet"]["categoryId"], put["status"]["privacyStatus"]), ("17", "unlisted"))
        self.assertEqual(self.tokens.saved("youtube")["broadcast"], "NEW")

    def test_youtube_updates_a_live_stream(self):
        self.settings.set_token("youtube", json.dumps({"refresh": "r", "stream_id": "S1"}))
        def call(url, token, method="GET", body=None, headers=None, raw=None):
            self.calls.append((method, url, body))
            if "broadcastStatus=active" in url:
                return {"items": [{"id": "LIVE", "contentDetails": {"boundStreamId": "S1"}}]}
            if "/videos?part=snippet,status&id=" in url:
                return {"items": [{"id": "LIVE", "snippet": {"title": "x"}}]}
            return {}
        self.si._call = call
        msg = self.si.push_youtube(self.tokens, new_destination("youtube"), self.si.merged({"title": "New title"}))
        self.assertIn("updated", msg)
        self.assertFalse(any("bind" in u or m == "DELETE" for m, u, b in self.calls))

    def test_pusher_leaves_platforms_alone_without_a_title(self):
        p = self.si.Pusher(self.settings, self.tokens)
        self.settings.update_destination({"id": "twitch", "enabled": True})
        self.settings.set_token("twitch", "refresh")
        p.push()
        self.assertEqual(p.info(), {})


@unittest.skipUnless(os.path.isfile(tool("mediamtx")) and shutil.which(tool("ffmpeg")),
                     "needs MediaMTX and FFmpeg (python tools/fetch_binaries.py)")
class EndToEndTest(unittest.TestCase):
    """A test pattern into the RTMP server, out through a copied and a
    cropped destination that point back at the same server."""

    def test_relay(self):
        work = tempfile.mkdtemp()
        settings = Settings(os.path.join(work, "config.json"))
        out = "rtmp://127.0.0.1:19361/out"
        settings.data["destinations"] = [
            new_destination("custom", id="a", name="A", enabled=True, server=out, key="a"),
            new_destination("custom", id="b", name="B", enabled=True, server=out, key="b", video=CROP, bitrate=1500),
        ]
        relay = Relay(settings, Ingest(rtmp_port=19361, api_port=19997, data_dir=work))
        relay.start()
        pub = procs.popen([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-re",
                           "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine",
                           "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-t", "60", "-f", "flv", "rtmp://127.0.0.1:19361/live/main"])
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if {"out/a", "out/b"} <= set(relay.ingest.paths) and \
                        all(o["status"] == "live" for o in relay.state()["outputs"]):
                    break
                time.sleep(1)
            state = relay.state()
            self.assertEqual(state["ingest"]["status"], "live", json.dumps(state))
            self.assertTrue({"out/a", "out/b"} <= set(relay.ingest.paths), json.dumps(state))
            self.assertTrue(all(o["status"] == "live" for o in state["outputs"]), json.dumps(state))
            # OBS stops: the outputs stop with it.
            procs.stop(pub)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and relay.state()["ingest"]["status"] == "live":
                time.sleep(1)
            time.sleep(1.5)
            self.assertEqual({o["status"] for o in relay.state()["outputs"]}, {"waiting"})
        finally:
            procs.stop(pub)
            relay.stop()
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
