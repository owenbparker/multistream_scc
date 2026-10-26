r"""Downloads the programs the app bundles into bin/: MediaMTX (the RTMP
server OBS streams to) and FFmpeg (sends the stream on to each platform).
Pinned versions, checked against their SHA-256.

    python tools/fetch_binaries.py            Windows builds (what the release ships)
    python tools/fetch_binaries.py --linux    MediaMTX for Linux, to run from source there
                                              (FFmpeg then comes from the system's PATH)
"""

import hashlib
import io
import os
import sys
import tarfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")

MEDIAMTX = "v1.9.3"
FFMPEG = "7.1"
WINDOWS = [
    (f"https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX}/mediamtx_{MEDIAMTX}_windows_amd64.zip",
     "af2ce0dce3201e10c39dae4e6d8e52c983d6940b1fa0fdd7f3d16c87ac764626",
     {"mediamtx.exe": "mediamtx.exe", "LICENSE": "licenses/MediaMTX-LICENSE.txt"}),
    # gyan.dev's "essentials" build (has the NVIDIA, AMD and Intel encoders), via its GitHub mirror.
    (f"https://github.com/GyanD/codexffmpeg/releases/download/{FFMPEG}/ffmpeg-{FFMPEG}-essentials_build.zip",
     "fa7d4d7e795db0e2503f49f105f46ed5852386f0cfdd819899be3b65ebde24fc",
     {f"ffmpeg-{FFMPEG}-essentials_build/bin/ffmpeg.exe": "ffmpeg.exe",
      f"ffmpeg-{FFMPEG}-essentials_build/LICENSE": "licenses/FFmpeg-LICENSE.txt"}),
]
LINUX = [
    (f"https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX}/mediamtx_{MEDIAMTX}_linux_amd64.tar.gz",
     None, {"mediamtx": "mediamtx"}),
]


def fetch(url, sha256, files):
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    digest = hashlib.sha256(data).hexdigest()
    if sha256 and digest != sha256:
        raise SystemExit(f"Checksum mismatch for {url}: got {digest}")
    if url.endswith(".zip"):
        archive = zipfile.ZipFile(io.BytesIO(data))
        read = archive.read
    else:
        archive = tarfile.open(fileobj=io.BytesIO(data))
        read = lambda name: archive.extractfile(name).read()
    for name, dest in files.items():
        path = os.path.join(BIN, dest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(read(name))
        if not path.endswith((".exe", ".txt")):
            os.chmod(path, 0o755)
        print(f"  bin/{dest}")


if __name__ == "__main__":
    for item in LINUX if "--linux" in sys.argv else WINDOWS:
        fetch(*item)
