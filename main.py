import http.server
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

SOURCE_URL = os.environ.get(
    "SOURCE_URL",
    "https://agmipocq.live.captain.vpplayer.net/hub/D188B8A9-8433-4A86-95FB-DB6BCAEC9BFA/tracks-v2a1/mono.ts.m3u8",
)
RTMP_URL = os.environ.get("RTMP_URL", "")
STREAM_KEY = os.environ.get("STREAM_KEY", "")
REFERER = os.environ.get(
    "REFERER", "https://host.vpplayer.tech/player/ptkzusga/vjsoalga.html"
)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")
FFMPEG_LOGLEVEL = os.environ.get("FFMPEG_LOGLEVEL", "info")
MIN_WAIT_SEC = int(os.environ.get("MIN_WAIT_SEC", "15"))
MAX_WAIT_SEC = int(os.environ.get("MAX_WAIT_SEC", "300"))
BACKOFF_MAX = int(os.environ.get("BACKOFF_MAX", "60"))
MIN_BACKOFF = int(os.environ.get("MIN_BACKOFF", "3"))
RESTART_MINUTES = int(os.environ.get("RESTART_MINUTES", "45"))
STALL_SECONDS = int(os.environ.get("STALL_SECONDS", "60"))
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "0"))
SEGMENT_DURATION = 4.2
TIME_RE = re.compile(rb"time=\s*\d+:\d+:\d+(\.\d+)?")
HEARTBEAT_PORT = int(os.environ.get("HEARTBEAT_PORT") or os.environ.get("PORT") or "0")
SELF_URL = os.environ.get("SELF_URL", "")

stop = threading.Event()


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch_headers():
    return {"User-Agent": USER_AGENT, "Referer": REFERER}


def is_live():
    try:
        req = urllib.request.Request(SOURCE_URL, headers=fetch_headers())
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as exc:
        log(f"source check failed: {exc}")
        return False


def build_rtmp():
    rtmp = RTMP_URL
    if STREAM_KEY and not rtmp.rstrip("/").endswith(STREAM_KEY):
        rtmp = f"{rtmp.rstrip('/')}/{STREAM_KEY}"
    return rtmp


def start_ffmpeg(rtmp):
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        FFMPEG_LOGLEVEL,
        "-user_agent",
        USER_AGENT,
        "-headers",
        f"Referer: {REFERER}\r\n",
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",
    ]
    if DELAY_SECONDS > 0:
        segments = max(1, int(math.ceil(DELAY_SECONDS / SEGMENT_DURATION)))
        cmd += ["-re", "-live_start_index", f"-{segments}"]
        log(f"output delay enabled: ~{segments * SEGMENT_DURATION:.0f}s ({DELAY_SECONDS}s requested)")
    else:
        cmd += ["-live_start_index", "-3"]
    cmd += [
        "-fflags",
        "+genpts",
        "-i",
        SOURCE_URL,
        "-c",
        "copy",
        "-f",
        "flv",
        "-flvflags",
        "no_duration_filesize",
        rtmp,
    ]
    log("starting ffmpeg: " + " ".join(cmd))
    return subprocess.Popen(cmd, stderr=subprocess.PIPE)


def monitor_progress(proc, stall_event):
    last = time.monotonic()
    for line in proc.stderr:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        if TIME_RE.search(line):
            last = time.monotonic()
        elif not stall_event.is_set() and time.monotonic() - last > STALL_SECONDS:
            stall_event.set()
    return


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def any_method(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = any_method

    def log_message(self, *args):
        pass


def serve_health():
    http.server.ThreadingHTTPServer(("0.0.0.0", HEARTBEAT_PORT), HealthHandler).serve_forever()


def self_ping():
    while not stop.wait(600):
        try:
            with urllib.request.urlopen(SELF_URL, timeout=15) as resp:
                log(f"self ping: {resp.status}")
        except Exception as exc:
            log(f"self ping failed: {exc}")


def handle_signal(signum, frame):
    log(f"received signal {signum}, shutting down")
    stop.set()


def main():
    if not RTMP_URL:
        log("RTMP_URL is required")
        sys.exit(1)

    log(f"source: {SOURCE_URL}")
    rtmp = build_rtmp()

    if HEARTBEAT_PORT:
        threading.Thread(target=serve_health, daemon=True).start()
        log(f"health endpoint on port {HEARTBEAT_PORT}")
    if SELF_URL:
        threading.Thread(target=self_ping, daemon=True).start()

    backoff = MIN_BACKOFF
    offline_wait = MIN_WAIT_SEC
    while not stop.is_set():
        if not is_live():
            log(f"source offline, reconnect attempt in {offline_wait}s")
            stop.wait(offline_wait)
            offline_wait = min(offline_wait * 2, MAX_WAIT_SEC)
            continue

        offline_wait = MIN_WAIT_SEC
        proc = start_ffmpeg(rtmp)
        started = time.monotonic()
        stalled = threading.Event()
        threading.Thread(target=monitor_progress, args=(proc, stalled), daemon=True).start()
        while proc.poll() is None and not stop.is_set():
            if stalled.wait(5):
                log(f"no ffmpeg progress for {STALL_SECONDS}s, restarting")
                proc.terminate()
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if RESTART_MINUTES and time.monotonic() - started > RESTART_MINUTES * 60:
                log(f"scheduled restart after {RESTART_MINUTES} minutes")
                proc.terminate()
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

        if stop.is_set():
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
            log("stopped")
            return

        rc = proc.returncode
        if rc == 0:
            log("stream ended naturally, checking source again")
            backoff = 5
        else:
            log(f"ffmpeg exited with code {rc}, retry in {backoff}s")
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

main()