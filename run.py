#!/usr/bin/env python3
"""Start Hydra server and GitHub poller in one command.

Usage:
    python run.py                # default: port 8000, poll every 30s
    python run.py --port 8080    # custom port
    python run.py --poll 10      # poll GitHub every 10s
    python run.py --no-poll      # skip GitHub poller
"""

import subprocess
import sys
import signal
import os

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run Hydra server + GitHub poller")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--poll", type=int, default=5, help="GitHub poll interval in seconds (default: 5)")
    parser.add_argument("--no-poll", action="store_true", help="Don't start the GitHub poller")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/static/index.html"

    print(f"\n  Hydra Demo")
    print(f"  ----------")
    print(f"  UI:  {url}")
    print(f"  API: http://{args.host}:{args.port}/api")
    if not args.no_poll:
        print(f"  GitHub poller: every {args.poll}s")
    print()

    procs = []

    # Start uvicorn server
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", args.host, "--port", str(args.port)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    procs.append(server)

    # Start GitHub poller
    poller = None
    if not args.no_poll:
        poller = subprocess.Popen(
            [sys.executable, "scripts/poll_github.py", "--interval", str(args.poll)],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        procs.append(poller)

    def shutdown(signum, frame):
        print("\nShutting down...")
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Wait for server — if it exits, stop everything
    server.wait()
    if poller and poller.poll() is None:
        poller.terminate()


if __name__ == "__main__":
    main()
