"""Local dev helper for the LiveKit webhook path.

Starts the FastAPI receiver AND a public tunnel to it, then prints the exact URL
to paste into the LiveKit Cloud dashboard. Leave it running while you drive calls.

    python scripts/webhook_dev.py

Needs a tunnel binary on PATH - `cloudflared` (preferred, no account) or `ngrok`.
Install cloudflared:
    winget install --id Cloudflare.cloudflared        (Windows)
    brew install cloudflared                          (macOS)
    https://github.com/cloudflare/cloudflared/releases (direct)

Then, one time, in the LiveKit Cloud dashboard:
    Project -> Settings -> Webhooks -> add the URL this script prints
    (it ends in /livekit/webhook). Save.

Verify it's flowing: run a call (scripts/sim_call.py) and then
    python scripts/check_webhook.py
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

PORT = int(os.environ.get("RECEIVER_PORT", "8080"))
_URL_RE = re.compile(r"https://[a-z0-9-]+\.(?:trycloudflare\.com|ngrok[-a-z.]*\.app|ngrok\.io)")


def _find_tunnel() -> tuple[str, list[str]]:
    if shutil.which("cloudflared"):
        return "cloudflared", ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"]
    if shutil.which("ngrok"):
        return "ngrok", ["ngrok", "http", str(PORT), "--log", "stdout"]
    sys.exit(
        "No tunnel binary found. Install cloudflared:\n"
        "  winget install --id Cloudflare.cloudflared   (Windows)\n"
        "  brew install cloudflared                     (macOS)\n"
        "  https://github.com/cloudflare/cloudflared/releases"
    )


def main() -> None:
    name, cmd = _find_tunnel()
    procs: list[subprocess.Popen] = []

    def _shutdown(*_a):
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        sys.exit(0)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, _shutdown)
            except (ValueError, OSError):
                pass

    print(f"starting receiver on :{PORT} ...")
    procs.append(subprocess.Popen([sys.executable, "-m", "receiver.app"], cwd=REPO))
    time.sleep(2)

    print(f"starting {name} tunnel ...")
    tunnel = subprocess.Popen(
        cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    procs.append(tunnel)

    public = None
    deadline = time.time() + 30
    for line in tunnel.stdout:  # type: ignore[union-attr]
        m = _URL_RE.search(line)
        if m:
            public = m.group(0)
            break
        if time.time() > deadline:
            break

    if not public:
        _shutdown()

    hook = f"{public}/livekit/webhook"
    print("\n" + "=" * 70)
    print("  Paste this into LiveKit Cloud:")
    print("    Project -> Settings -> Webhooks -> add:")
    print(f"    {hook}")
    print("=" * 70)
    print("\n  Leave this running. Ctrl+C to stop.")
    print("  After a call:  python scripts/check_webhook.py\n")

    tunnel.wait()
    _shutdown()


if __name__ == "__main__":
    main()
