"""The relay: MediaMTX takes OBS's stream in, and one FFmpeg per platform
sends it on.

    OBS  --rtmp-->  MediaMTX (port 1935)  --+--> ffmpeg --> YouTube
                                            +--> ffmpeg --> Twitch
                                            +--> ffmpeg --> ...

Each FFmpeg copies the stream as it is (no re-encode), except a cropped
vertical output, which re-encodes the middle of the picture as 9:16 (on the
graphics card when there is one). A watcher checks every second: when OBS
starts streaming, every platform that's switched on goes live; one that
drops is retried (2 s, 4 s, ... up to 30 s apart) while the others carry on;
when OBS stops, they all stop.
"""

import json
import os
import socket
import threading
import time
import urllib.request

from . import procs
from .config import API_PORT, DATA_DIR, RTMP_PORT, tool
from .log import log
from .settings import COPY, CROP, VERTICAL, target_url

VERTICAL_KEY = "vertical"
RETRY_DELAYS = (2, 4, 8, 15, 30)
STALL_SECONDS = 15        # live but nothing sent for this long: restart it
STEADY_SECONDS = 60       # live this long: the next drop retries quickly again
ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")   # tried in order, then libx264


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def port_free(port, host="0.0.0.0"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":   # a port left in TIME_WAIT by the last run is free (Windows allows that anyway)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def redact(text, dest):
    key = (dest.get("key") or "").strip()
    return text.replace(key, "••••") if len(key) >= 4 else text


# --- MediaMTX ----------------------------------------------------------------

def mediamtx_config(lan, rtmp_port=RTMP_PORT, api_port=API_PORT):
    host = "" if lan else "127.0.0.1"
    return f"""# Written by SCC Multistream each time it starts; edits here are replaced.
logLevel: info
logDestinations: [stdout]
api: yes
apiAddress: 127.0.0.1:{api_port}
metrics: no
pprof: no
playback: no
rtsp: no
hls: no
webrtc: no
srt: no
rtmp: yes
rtmpAddress: {host}:{rtmp_port}
rtmpEncryption: "no"
paths:
  all_others:
"""


class Ingest:
    """MediaMTX, and what's arriving from OBS."""

    def __init__(self, lan=False, rtmp_port=RTMP_PORT, api_port=API_PORT, data_dir=DATA_DIR):
        self.lan, self.rtmp_port, self.api_port, self.data_dir = lan, rtmp_port, api_port, data_dir
        self.proc = None
        self.error = None
        self.paths = {}            # name -> MediaMTX's info, for the paths OBS is publishing
        self.rates = {}            # name -> kbps
        self._last = {}            # name -> (time, bytesReceived)
        self._restarts = 0

    def start(self):
        procs.stop(self.proc)
        self.proc, self.paths = None, {}
        exe = tool("mediamtx")
        if not os.path.isfile(exe):
            self.error = f"MediaMTX is missing ({exe})"
            log(self.error, "error")
            return False
        host = "0.0.0.0" if self.lan else "127.0.0.1"
        if not port_free(self.rtmp_port, host):
            self.error = (f"Port {self.rtmp_port} is in use by another program (another RTMP server, "
                          "or SCC Multistream already open). Close it, then press Restart.")
            log(self.error, "error")
            return False
        if not port_free(self.api_port, "127.0.0.1"):
            self.error = f"Port {self.api_port} is in use by another program. Close it, then press Restart."
            log(self.error, "error")
            return False
        conf = os.path.join(self.data_dir, "mediamtx.yml")
        with open(conf, "w", encoding="utf-8") as f:
            f.write(mediamtx_config(self.lan, self.rtmp_port, self.api_port))
        self.proc = procs.popen([exe, conf], stdout=procs.subprocess.PIPE, stderr=procs.subprocess.STDOUT,
                                cwd=self.data_dir)
        threading.Thread(target=self._read_log, args=(self.proc,), daemon=True).start()
        self.error = None
        where = "this PC and the network" if self.lan else "this PC"
        log(f"RTMP server listening on port {self.rtmp_port} ({where})")
        return True

    def _read_log(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            # "2024/10/20 14:44:01 INF [RTMP] [conn 127.0.0.1:5000] is publishing to path 'live/main', ..."
            body = line.split(" ", 2)[-1] if line[:4].isdigit() else line
            if body.startswith(("ERR", "WAR")):
                log(f"RTMP server: {body[4:]}", "warn")
            elif "is publishing to path" in body:
                log(f"OBS connected: {body.split('] ', 2)[-1].split(',')[0]}")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def poll(self):
        """Refresh what's being published. Restarts MediaMTX if it died."""
        if self.proc is not None and not self.alive():
            self._restarts += 1
            log(f"RTMP server stopped (exit code {self.proc.returncode}); restarting", "warn")
            time.sleep(min(10, self._restarts))
            self.start()
            return
        if not self.alive():
            return
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.api_port}/v3/paths/list?itemsPerPage=100",
                                        timeout=2) as r:
                items = json.loads(r.read()).get("items") or []
        except Exception:
            return   # still starting
        now = time.monotonic()
        self.paths = {p["name"]: p for p in items if p.get("ready")}
        for name, p in self.paths.items():
            got = p.get("bytesReceived") or 0
            then, before = self._last.get(name, (now, got))
            if now - then >= 1:
                self.rates[name] = int((got - before) * 8 / 1000 / (now - then))
                self._last[name] = (now, got)
            else:
                self._last.setdefault(name, (now, got))
        for name in list(self._last):
            if name not in self.paths:
                self._last.pop(name)
                self.rates.pop(name, None)

    def source(self, vertical=False):
        """The path OBS's main (or vertical) output is publishing, or None.
        Any stream key works for the main one (rtmp://.../live is preferred);
        the vertical one uses "vertical"."""
        for name in sorted(self.paths, key=lambda n: (not n.startswith("live/"), n)):
            if (name.rsplit("/", 1)[-1] == VERTICAL_KEY) == vertical:
                return name
        return None

    def url(self, path):
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{path}"

    def stop(self):
        procs.stop(self.proc)
        self.proc, self.paths = None, {}


# --- Encoder for the cropped vertical output ---------------------------------

def find_encoder(ffmpeg=None):
    """The first H.264 encoder that works on this PC: NVIDIA, AMD, Intel, else the CPU."""
    ffmpeg = ffmpeg or tool("ffmpeg")
    for enc in ENCODERS:
        try:
            code, _ = procs.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                                 "-i", "color=c=black:s=1280x720:r=30", "-frames:v", "5",
                                 "-vf", "format=nv12", "-c:v", enc, "-f", "null", "-"], timeout=20)
        except Exception:
            continue
        if code == 0:
            return enc
    return "libx264"


def encoder_args(enc, kbps):
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    return {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "cbr"] + rate,
        "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cbr"] + rate,
        "h264_qsv": ["-c:v", "h264_qsv", "-preset", "medium"] + rate,
    }.get(enc, ["-c:v", "libx264", "-preset", "veryfast"] + rate)


# The middle of a 16:9 picture as 9:16: 1080x1920 from 1440p and up, else 720x1280.
CROP_FILTER = ("crop=trunc(ih*9/16/2)*2:ih,"
               "scale=w='if(gte(ih,1440),1080,720)':h='if(gte(ih,1440),1920,1280)',format=nv12")


def ffmpeg_command(dest, source_url, encoder="libx264", ffmpeg=None):
    args = [ffmpeg or tool("ffmpeg"), "-hide_banner", "-nostats", "-loglevel", "error",
            "-progress", "pipe:1", "-rw_timeout", "10000000", "-i", source_url]
    if dest.get("video") == CROP:
        args += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", CROP_FILTER] + encoder_args(encoder, int(dest.get("bitrate") or 6000))
        args += ["-force_key_frames", "expr:gte(t,n_forced*2)", "-c:a", "copy"]
    else:
        args += ["-map", "0", "-c", "copy"]
    args += ["-f", "flv", "-flvflags", "no_duration_filesize", target_url(dest)]
    return args


# --- One platform ------------------------------------------------------------

def explain(lines, code):
    """FFmpeg's last words -> what to do about it."""
    text = " ".join(lines).lower()
    if any(s in text for s in ("connection refused", "failed to resolve", "name or service not known",
                               "no such host", "timed out", "cannot assign")):
        return "Couldn't reach the server - check the server address"
    if "tls" in text or "handshake" in text or "ssl" in text:
        return "Secure (rtmps) connection failed - check the server address"
    if any(s in text for s in ("input/output error", "broken pipe", "connection reset", "end of file")):
        return "The platform closed the connection - check the stream key, and that the stream is set up there"
    for line in reversed(lines):
        if not line.lower().startswith(("error opening output file", "conversion failed")):
            return line.split("] ", 1)[-1]
    return lines[-1] if lines else f"FFmpeg stopped (exit code {code})"


class Output:
    """One FFmpeg sending to one platform, with its status."""

    def __init__(self, dest):
        self.dest = dest
        self.proc = None
        self.signature = None
        self.status, self.message = "off", ""
        self.kbps = 0
        self.live_since = None
        self.retry_at = 0
        self.failures = 0
        self.stderr = []
        self._bytes = (0, 0.0)          # (total_size, time) at the last rate sample
        self._last_growth = 0.0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args):
        self.stderr, self.kbps, self.live_since = [], 0, None
        now = time.monotonic()
        self._bytes, self._last_growth = (0, now), now
        self.status, self.message = "starting", "Connecting…"
        self.proc = procs.popen(args, stdout=procs.subprocess.PIPE, stderr=procs.subprocess.PIPE)
        threading.Thread(target=self._read_progress, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_errors, args=(self.proc,), daemon=True).start()

    def _read_progress(self, proc):
        size = 0
        for raw in proc.stdout:
            key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
            if key == "total_size" and value.isdigit():
                size = int(value)
            elif key == "progress" and proc is self.proc:
                self._sample(size)

    def _sample(self, size):
        now = time.monotonic()
        last_size, last_time = self._bytes
        if size > last_size:
            self._last_growth = now
            if self.live_since is None:
                self.live_since = time.time()
                self.status, self.message = "live", ""
                log(f"{self.dest['name']}: live")
        if now - last_time >= 2:
            self.kbps = int((size - last_size) * 8 / 1000 / (now - last_time))
            self._bytes = (size, now)

    def _read_errors(self, proc):
        for raw in proc.stderr:
            line = redact(raw.decode("utf-8", "replace").strip(), self.dest)
            if line:
                self.stderr = (self.stderr + [line])[-8:]

    def stalled(self):
        return self.live_since is not None and time.monotonic() - self._last_growth > STALL_SECONDS

    def stop(self, status="off", message=""):
        proc, self.proc = self.proc, None
        procs.stop(proc)
        self.status, self.message, self.kbps, self.live_since = status, message, 0, None

    def failed(self, why):
        """It stopped on its own (or stalled): try again after a growing delay."""
        steady = self.live_since is not None and time.time() - self.live_since > STEADY_SECONDS
        self.failures = 1 if steady else self.failures + 1
        delay = RETRY_DELAYS[min(self.failures, len(RETRY_DELAYS)) - 1]
        self.stop("retrying", why)
        self.retry_at = time.monotonic() + delay
        detail = f" [{self.stderr[-1]}]" if self.stderr and self.stderr[-1] not in why else ""
        log(f"{self.dest['name']}: {why}{detail} - retrying in {delay} s", "warn")

    def info(self):
        wait = max(0, int(self.retry_at - time.monotonic() + .99)) if self.status == "retrying" else 0
        return {"id": self.dest["id"], "status": self.status, "message": self.message, "kbps": self.kbps,
                "live_since": self.live_since, "retry_in": wait}


# --- The watcher -------------------------------------------------------------

class Relay:
    def __init__(self, settings, ingest=None):
        self.settings = settings
        self.ingest = ingest or Ingest(lan=settings.snapshot()["lan"])
        self.outputs = {}
        self.encoder = None
        self.ingest_since = {}            # path -> when OBS started sending it
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        self.ingest.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        threading.Thread(target=self._pick_encoder, daemon=True).start()

    def _pick_encoder(self):
        chosen = self.settings.snapshot().get("encoder") or "auto"
        self.encoder = find_encoder() if chosen == "auto" else chosen
        log(f"Vertical crop encodes with {self.encoder}")

    def restart(self, lan=None):
        """Restart the RTMP server (e.g. after changing who can stream in)."""
        with self._lock:
            for out in self.outputs.values():
                out.stop()
            if lan is not None:
                self.ingest.lan = lan
            self.ingest.stop()
            self.ingest.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        with self._lock:
            for out in self.outputs.values():
                out.stop()
            self.ingest.stop()

    def poke(self):
        """Apply a settings change now rather than on the next second."""
        with self._lock:
            self.tick()

    def _watch(self):
        while not self._stop.is_set():
            try:
                self.ingest.poll()
                with self._lock:
                    self.tick()
            except Exception as e:  # never let the watcher die
                log(f"Relay error: {type(e).__name__}: {e}", "error")
            self._stop.wait(1)

    def tick(self):
        """Bring each platform's FFmpeg in line with the settings and with
        what OBS is sending. Called every second."""
        now = time.monotonic()
        for path in list(self.ingest_since):
            if path not in self.ingest.paths:
                log(f"OBS stopped sending {path}")
                self.ingest_since.pop(path)
        for path in self.ingest.paths:
            self.ingest_since.setdefault(path, time.time())

        dests = {d["id"]: d for d in self.settings.destinations()}
        for gone in set(self.outputs) - set(dests):
            self.outputs.pop(gone).stop()
        for dest_id, dest in dests.items():
            out = self.outputs.get(dest_id)
            if out is None:
                out = self.outputs[dest_id] = Output(dest)
            out.dest = dest
            self._tick_output(out, now)

    def _tick_output(self, out, now):
        dest = out.dest
        if not dest["enabled"]:
            if out.running() or out.status != "off":
                if out.running():
                    log(f"{dest['name']}: stopped")
                out.stop()
                out.failures = 0
            return
        if not dest.get("server"):
            out.stop("setup", "Add the server and stream key")
            return
        vertical = dest.get("video") == VERTICAL
        source = self.ingest.source(vertical=vertical)
        if source is None:
            if out.running():
                log(f"{dest['name']}: stopped (OBS isn't streaming)")
            waiting = ("Waiting for OBS's vertical output (stream key \"vertical\")" if vertical
                       else "Waiting for OBS")
            out.stop("waiting", waiting)
            out.failures = 0
            return
        if dest.get("video") == CROP and not self.encoder:
            out.stop("starting", "Finding the best encoder…")
            return
        args = ffmpeg_command(dest, self.ingest.url(source), self.encoder or "libx264")
        if out.running():
            if out.signature != args:          # settings changed while live
                log(f"{dest['name']}: settings changed, reconnecting")
                out.stop()
            elif out.stalled():
                out.failed(f"No data sent for {STALL_SECONDS} s")
                return
            else:
                return
        elif out.proc is not None:             # FFmpeg ended on its own
            out.failed(explain(out.stderr, out.proc.returncode))
            return
        if out.status == "retrying" and now < out.retry_at:
            return
        try:
            out.start(args)
            out.signature = args
            log(f"{dest['name']}: connecting")
        except OSError as e:
            out.proc = None
            out.failed(f"Couldn't start FFmpeg ({e})")

    def state(self):
        ing = self.ingest
        main, vert = ing.source(), ing.source(vertical=True)
        if ing.error:
            status = "error"
        elif not ing.alive():
            status = "starting"
        else:
            status = "live" if main else "waiting"
        outputs = [o.info() for o in self.outputs.values()]
        host = lan_ip() if ing.lan else "127.0.0.1"
        return {
            "ingest": {"status": status, "error": ing.error, "path": main, "kbps": ing.rates.get(main, 0),
                       "tracks": (ing.paths.get(main) or {}).get("tracks") or [],
                       "since": self.ingest_since.get(main),
                       "server": f"rtmp://127.0.0.1:{ing.rtmp_port}/live",
                       "lan_server": f"rtmp://{host}:{ing.rtmp_port}/live" if ing.lan else None,
                       "key": "main", "vertical_key": VERTICAL_KEY, "lan": ing.lan,
                       "vertical": {"path": vert, "kbps": ing.rates.get(vert, 0)} if vert else None},
            "outputs": outputs,
            "encoder": self.encoder,
            "upload_kbps": sum(o["kbps"] for o in outputs),
        }
