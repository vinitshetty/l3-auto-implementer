#!/usr/bin/env python3
"""Poll GitHub for CI status and PR reviews, then send signals to running workflows.
Run this alongside the server when webhooks aren't configured.

Usage:
    python scripts/poll_github.py                  # poll every 30s
    python scripts/poll_github.py --interval 10    # poll every 10s
    python scripts/poll_github.py --once           # single poll then exit
"""

import argparse
import time
import httpx
import os
import sys

API_BASE = os.environ.get("HYDRA_API_URL", "http://127.0.0.1:8000")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Track what we've already signaled to avoid duplicates
_signaled_ci: set[str] = set()      # session_id -> last CI conclusion
_signaled_reviews: set[str] = set()  # session_id:review_id


def get_github_headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def get_sessions():
    """Get all active sessions from the Hydra API."""
    resp = httpx.get(f"{API_BASE}/api/sessions", timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_live_state(session_id: str) -> dict | None:
    """Get live workflow state."""
    try:
        resp = httpx.get(f"{API_BASE}/api/sessions/{session_id}/live", timeout=30)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def check_ci_status(session: dict, live: dict):
    """Check GitHub CI status for a session's branch and send signal if completed."""
    pr_url = live.get("pr_url") or session.get("pr_url")
    branch = live.get("branch_name") or session.get("branch_name")
    repo_url = session.get("repo_url", "")

    if not branch or not repo_url:
        return

    parts = repo_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]

    # Get check runs for the branch
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}/check-runs",
        headers=get_github_headers(),
        timeout=30,
    )
    if resp.status_code != 200:
        return

    check_runs = resp.json().get("check_runs", [])
    if not check_runs:
        # Also check commit status (some CI uses status API)
        resp2 = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}/status",
            headers=get_github_headers(),
            timeout=30,
        )
        if resp2.status_code == 200:
            status = resp2.json()
            state = status.get("state", "")  # success, failure, pending
            if state in ("success", "failure"):
                conclusion = state
                sig_key = f"{session['id']}:status:{conclusion}"
                if sig_key not in _signaled_ci:
                    send_ci_signal(session["id"], conclusion)
                    _signaled_ci.add(sig_key)
        return

    # Check if all check runs are completed
    all_completed = all(cr.get("status") == "completed" for cr in check_runs)
    if not all_completed:
        return

    # Determine overall conclusion
    conclusions = [cr.get("conclusion", "") for cr in check_runs]
    if all(c == "success" for c in conclusions):
        overall = "success"
    else:
        overall = "failure"

    sig_key = f"{session['id']}:checks:{overall}:{len(check_runs)}"
    if sig_key not in _signaled_ci:
        print(f"  CI {overall} for {session['id'][:8]} ({len(check_runs)} checks)")
        send_ci_signal(session["id"], overall)
        _signaled_ci.add(sig_key)


def check_pr_reviews(session: dict, live: dict):
    """Check PR reviews and send signal if new review found."""
    pr_url = live.get("pr_url") or session.get("pr_url")
    if not pr_url:
        return

    repo_url = session.get("repo_url", "")
    parts = repo_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]

    # Extract PR number from URL
    pr_number = pr_url.rstrip("/").split("/")[-1]

    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers=get_github_headers(),
        timeout=30,
    )
    if resp.status_code != 200:
        return

    reviews = resp.json()
    for review in reviews:
        review_id = review.get("id")
        sig_key = f"{session['id']}:{review_id}"
        if sig_key in _signaled_reviews:
            continue

        state = review.get("state", "").lower()  # approved, changes_requested, commented
        if state in ("approved", "changes_requested"):
            print(f"  PR review '{state}' for {session['id'][:8]} by {review.get('user', {}).get('login', '?')}")
            send_review_signal(session["id"], state, review)
            _signaled_reviews.add(sig_key)


def send_ci_signal(session_id: str, conclusion: str):
    """Send CI result signal to the workflow."""
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions/{session_id}/signal/ci",
            params={"conclusion": conclusion},
            timeout=30,
        )
        print(f"    -> CI signal sent: {resp.json()}")
    except Exception as e:
        print(f"    -> CI signal failed: {e}")


def send_review_signal(session_id: str, action: str, review: dict):
    """Send review feedback signal to the workflow."""
    try:
        # Use the webhook endpoint format
        comments = []
        if action == "changes_requested":
            comments = [{"author": review.get("user", {}).get("login", ""), "body": review.get("body", "")}]

        # Direct signal via assist/cancel won't work for reviews,
        # so we add a review signal endpoint call
        resp = httpx.post(
            f"{API_BASE}/api/sessions/{session_id}/signal/review",
            json={"action": action, "comments": comments},
            timeout=30,
        )
        print(f"    -> Review signal sent: {resp.json()}")
    except Exception as e:
        print(f"    -> Review signal failed: {e}")


def check_pr_comments(session: dict, live: dict):
    """Check PR issue comments and send as review signal if new ones found."""
    pr_url = live.get("pr_url") or session.get("pr_url")
    if not pr_url:
        return

    repo_url = session.get("repo_url", "")
    parts = repo_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]

    pr_number = pr_url.rstrip("/").split("/")[-1]

    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        headers=get_github_headers(),
        timeout=30,
    )
    if resp.status_code != 200:
        return

    comments = resp.json()
    for comment in comments:
        comment_id = comment.get("id")
        sig_key = f"{session['id']}:comment:{comment_id}"
        if sig_key in _signaled_reviews:
            continue

        author = comment.get("user", {}).get("login", "")
        body = comment.get("body", "")

        # Skip bot comments
        if comment.get("user", {}).get("type") == "Bot":
            continue

        print(f"  PR comment from {author} for {session['id'][:8]}: {body[:80]}")
        send_review_signal(session["id"], "changes_requested", {
            "user": {"login": author},
            "body": body,
        })
        _signaled_reviews.add(sig_key)


def poll_once():
    """Single poll iteration."""
    sessions = get_sessions()
    terminal = {"completed", "failed", "cancelled", "error"}
    active = [s for s in sessions if s.get("workflow_run_id") and s.get("status") not in terminal]

    if not active:
        return

    for session in active:
        live = get_live_state(session["id"])
        if not live:
            continue

        status = live.get("status", "")

        if status == "ci_monitoring":
            check_ci_status(session, live)
            # Also check for PR comments/reviews while waiting for CI
            check_pr_comments(session, live)
            check_pr_reviews(session, live)

        if status == "pr_review":
            check_pr_reviews(session, live)
            check_pr_comments(session, live)


def main():
    parser = argparse.ArgumentParser(description="Poll GitHub for CI/review status")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run once then exit")
    args = parser.parse_args()

    # Load token from .env if not in environment
    global GITHUB_TOKEN
    if not GITHUB_TOKEN:
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("GITHUB_TOKEN="):
                    GITHUB_TOKEN = line.strip().split("=", 1)[1]
                    break

    if not GITHUB_TOKEN:
        print("Warning: No GITHUB_TOKEN found. GitHub API calls may be rate-limited.")

    print(f"Polling GitHub for CI status and PR reviews (every {args.interval}s)")
    print(f"API: {API_BASE}")

    if args.once:
        poll_once()
        return

    while True:
        try:
            poll_once()
        except KeyboardInterrupt:
            print("\nStopping.")
            break
        except Exception as e:
            print(f"Poll error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
