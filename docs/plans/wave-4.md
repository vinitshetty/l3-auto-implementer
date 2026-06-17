# Wave 4: GitHub Integration + Webhook Signals

> **Prerequisites:** Wave 3 complete (triage workflow, session workflow, activities exist).
> **Architecture context:** See `hydra-architecture.md` for webhook signal mapping, API contracts.

---

## Task 7: GitHub Client

**Files:** `app/integrations/github_client.py`, `tests/test_github_client.py`

- [ ] **Step 1:** Write failing tests for:
  - `get_issue(owner, repo, issue_number)` → {title, body, labels, state, url}
  - `get_repo_tree(owner, repo, path?)` → file/dir listing (used by triage agent)
  - `get_file_content(owner, repo, path)` → file contents (used by triage agent)
  - `search_code(owner, repo, query)` → matching files + snippets (used by triage agent)
  - `get_recent_commits(owner, repo, path?, limit?)` → commit list
  - `search_issues(owner, repo, query, state?)` → similar issues
  - `add_labels(owner, repo, issue_number, labels)` → adds labels
  - `create_issue_comment(owner, repo, issue_number, body)` → posts comment
  - `create_pull_request(owner, repo, head, base, title, body)` → {pr_url, pr_number} — body includes `Fixes #N` or `Implements #N` when issue linked
  - `get_check_runs(owner, repo, ref)` → list of {name, status, conclusion, details_url}
  - `get_pr_status(owner, repo, pr_number)` → {state, merged, mergeable, review_comments}
- [ ] **Step 2:** Implement with httpx async client. Add a `_request` wrapper that accepts optional `tracer: Tracer` + `parent_span: Span` — when provided, wraps each HTTP call in an `http` span with: url, method, status_code, response_bytes. This means every GitHub API call made from activities or triage functions automatically appears in the trace tree.
- [ ] **Step 3:** Run tests — verify PASS
- [ ] **Step 4:** Commit

---

## Task 8: Webhook Handler (Signal Relay to Workflows)

**Files:** `app/api/webhooks.py`, `tests/test_webhooks.py`

The webhook handler is a pure signal relay — it does zero business logic. It verifies, parses, and forwards to the correct workflow.

- [ ] **Step 1:** Write failing test — `issues.opened` webhook → starts Triage Workflow
- [ ] **Step 2:** Write failing test — `check_suite.completed` webhook → sends `ci_result` signal to Session Workflow
- [ ] **Step 3:** Write failing test — `pull_request_review.submitted` with "changes_requested" → sends `review_feedback` signal to Session Workflow
- [ ] **Step 4:** Write failing test — `pull_request_review.submitted` with "approved" → sends `review_feedback` signal (empty comments) to Session Workflow
- [ ] **Step 5:** Write failing test — invalid HMAC signature returns 403
- [ ] **Step 6:** Implement `webhooks.py`:
  - HMAC-SHA256 signature verification
  - Route by event type:
    - `issues.opened` → start Triage Workflow with repo_url + issue_number
    - `check_suite` / `check_run` completed → look up session by branch → `session_workflow.signal("ci_result", payload)`
    - `pull_request_review.submitted` → look up session by PR → `session_workflow.signal("review_feedback", payload)`
- [ ] **Step 7:** Run tests — verify PASS
- [ ] **Step 8:** Commit
