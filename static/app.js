// Hydra Demo — Vanilla JS client

const API = '/api';

// --- API helpers ---

async function api(path, opts = {}) {
  const headers = opts.body ? { 'Content-Type': 'application/json' } : {};
  const resp = await fetch(`${API}${path}`, {
    headers,
    ...opts,
  });
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

// --- Sessions ---

async function loadSessions(status) {
  const params = status && status !== 'all' ? `?status=${status}` : '';
  const sessions = await api(`/sessions${params}`);
  const list = document.getElementById('session-list');
  if (!list) return;
  list.innerHTML = sessions.map(s => `
    <a href="/static/session.html?id=${s.id}" style="text-decoration:none;color:inherit">
      <div class="card">
        <div class="card-header">
          <span class="card-title">${escHtml(s.task_description.slice(0, 60))}</span>
          <span class="badge status-${s.status}">${s.status}</span>
        </div>
        <div class="card-meta">
          ${s.repo_url} &bull; Iter ${s.iteration_count}/${s.max_iterations}
          ${s.issue_number ? `&bull; #${s.issue_number}` : ''}
          ${s.triage_id ? '<span class="badge" style="background:var(--span-agent)">Triaged</span>' : ''}
        </div>
      </div>
    </a>
  `).join('');
}

function filterSessions(status, btn) {
  document.querySelectorAll('.filter-tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadSessions(status);
}

async function createSession(e) {
  e.preventDefault();
  const data = {
    repo_url: document.getElementById('repo_url').value,
    task_description: document.getElementById('task_description').value,
    max_iterations: parseInt(document.getElementById('max_iterations').value) || 3,
  };
  const issue = document.getElementById('issue_number').value;
  if (issue) data.issue_number = parseInt(issue);
  const type = document.getElementById('issue_type').value;
  if (type) data.issue_type = type;
  const session = await api('/sessions', { method: 'POST', body: JSON.stringify(data) });
  location.href = `/static/session.html?id=${session.id}`;
}

async function previewIssue() {
  const num = document.getElementById('issue_number').value;
  const repo = document.getElementById('repo_url').value;
  const el = document.getElementById('issue-preview');
  if (!num || !repo) { el.style.display = 'none'; return; }
  try {
    const data = await api(`/issues/preview?repo_url=${encodeURIComponent(repo)}&issue_number=${num}`);
    el.textContent = data.title ? `Issue: ${data.title}` : 'Issue not found';
    el.style.display = 'block';
    const taskDesc = document.getElementById('task_description');
    if (data.title) taskDesc.value = data.title;
  } catch { el.style.display = 'none'; }
}

// --- Session detail ---

async function loadSessionDetail(id) {
  // Fetch session (skip Temporal enrichment), live state, and trace in parallel
  const [s, live, trace] = await Promise.all([
    api(`/sessions/${id}?enrich=false`),
    api(`/sessions/${id}/live`).catch(() => null),
    api(`/sessions/${id}/trace`).catch(() => null),
  ]);

  // Merge live state into session data so UI reflects current workflow state
  if (live && typeof live === 'object') {
    if (live.status) s.status = live.status;
    if (live.branch_name) s.branch_name = live.branch_name;
    if (live.pr_url) s.pr_url = live.pr_url;
    if (live.iteration !== undefined) s.iteration_count = live.iteration;
    if (live.error_summary) s.error_summary = live.error_summary;
  }

  document.getElementById('session-title').textContent = s.task_description;
  document.getElementById('session-status').className = `badge status-${s.status}`;
  document.getElementById('session-status').textContent = s.status;
  document.getElementById('session-meta').innerHTML = `
    <a href="${s.repo_url}" target="_blank">${s.repo_url}</a>
    ${s.branch_name ? `&bull; ${s.branch_name}` : ''}
    ${s.issue_number ? `&bull; <a href="${s.repo_url}/issues/${s.issue_number}" target="_blank">#${s.issue_number}</a>` : ''}
    &bull; Iteration ${s.iteration_count}/${s.max_iterations}
  `;

  // Render trace immediately (fetched in parallel above)
  if (trace && trace.root_spans) renderTrace(trace.root_spans);

  // Load live state for panels
  try {
    if (!live) throw new Error('no live data');

    // Initialize DAG from live workflow status (no message, just status)
    if (live.status === 'running') {
      // Running could be prepare or code — SSE replay will set the right state
      dagSetPhase('prepare', 'active');
    } else if (live.status) {
      dagHandleSSE({ event_type: 'status_change', payload: live });
    }

    if (live.status === 'ci_monitoring') {
      const ciPanel = document.getElementById('ci-panel');
      if (ciPanel) ciPanel.style.display = 'block';
    }

    if (live.status === 'pr_review') {
      const reviewPanel = document.getElementById('review-panel');
      if (reviewPanel) reviewPanel.style.display = 'block';
    }

    if (live.status === 'failed' || live.status === 'cancelled') {
      const retryPanel = document.getElementById('retry-panel');
      if (retryPanel) {
        retryPanel.style.display = 'block';
        document.getElementById('retry-error').textContent = live.error_summary || `Session ${live.status}`;
      }
    }

    if (live.status === 'needs_human') {
      document.getElementById('assist-panel').style.display = 'block';
      document.getElementById('assist-context').textContent = live.error_summary || 'Agent is stuck';
    }

    // Test results
    if (live.test_results) updateTestPanel(live.test_results);

    // Confidence
    if (live.confidence) updateConfidencePanel(live.confidence);

    // PR info
    if (live.pr_url) updatePRPanel(live.pr_url);
  } catch (err) {
    console.error('Live state error:', err);
  }
}

async function sendAssist() {
  const guidance = document.getElementById('assist-guidance').value;
  if (!guidance) return;
  await api(`/sessions/${sessionId}/assist`, { method: 'POST', body: JSON.stringify({ guidance }) });
  document.getElementById('assist-panel').style.display = 'none';
}

async function cancelSession() {
  await api(`/sessions/${sessionId}/cancel`, { method: 'POST' });
  location.reload();
}

async function sendCISignal(conclusion) {
  try {
    await api(`/sessions/${sessionId}/signal/ci?conclusion=${conclusion}`, { method: 'POST' });
    location.reload();
  } catch (e) {
    alert('Signal failed: ' + e.message);
  }
}

async function sendReviewSignal(action) {
  try {
    await api(`/sessions/${sessionId}/signal/review`, {
      method: 'POST',
      body: JSON.stringify({ action, comments: [] }),
    });
    location.reload();
  } catch (e) {
    alert('Signal failed: ' + e.message);
  }
}

async function retrySession() {
  try {
    await api(`/sessions/${sessionId}/retry`, { method: 'POST' });
    location.reload();
  } catch (e) {
    alert('Retry failed: ' + e.message);
  }
}

// --- SSE ---

let _currentSSE = null;

function connectSSE(id) {
  // Close existing connection to prevent leak
  if (_currentSSE) {
    _currentSSE.close();
    _currentSSE = null;
  }

  const es = new EventSource(`${API}/sessions/${id}/events`);
  _currentSSE = es;
  const stream = document.getElementById('event-stream');

  es.onmessage = (e) => { const d = JSON.parse(e.data); appendEvent(stream, d); };
  es.addEventListener('status_change', (e) => {
    const data = JSON.parse(e.data);
    appendEvent(stream, data, 'event-status');
    dagHandleSSE(data);
    // Update status badge and panels in real-time
    const payload = data.payload || data;
    const newStatus = payload.status;
    if (newStatus) {
      const badge = document.getElementById('session-status');
      if (badge) { badge.textContent = newStatus; badge.className = `badge status-${newStatus}`; }
      // Show/hide action panels based on new status
      const ciPanel = document.getElementById('ci-panel');
      const reviewPanel = document.getElementById('review-panel');
      const assistPanel = document.getElementById('assist-panel');
      const retryPanel = document.getElementById('retry-panel');
      if (ciPanel) ciPanel.style.display = newStatus === 'ci_monitoring' ? 'block' : 'none';
      if (reviewPanel) reviewPanel.style.display = newStatus === 'pr_review' ? 'block' : 'none';
      if (assistPanel) assistPanel.style.display = newStatus === 'needs_human' ? 'block' : 'none';
      if (retryPanel) retryPanel.style.display = (newStatus === 'failed' || newStatus === 'cancelled') ? 'block' : 'none';
    }
    // Update data panels from SSE payload
    if (payload.test_results) updateTestPanel(payload.test_results);
    if (payload.confidence) updateConfidencePanel(payload.confidence);
    if (payload.pr_url) updatePRPanel(payload.pr_url);
  });
  es.addEventListener('error', (e) => { if (e.data) appendEvent(stream, JSON.parse(e.data), 'event-error'); });
  es.addEventListener('agent_message', (e) => appendEvent(stream, JSON.parse(e.data), 'event-agent'));
  es.addEventListener('human_assist_request', (e) => appendEvent(stream, JSON.parse(e.data), 'event-human'));
  es.addEventListener('span_start', (e) => appendEvent(stream, JSON.parse(e.data)));
  es.addEventListener('span_end', (e) => appendEvent(stream, JSON.parse(e.data)));
  es.addEventListener('test_summary', (e) => updateTestPanel(JSON.parse(e.data)));

  es.onerror = () => {
    es.close();
    _currentSSE = null;
    setTimeout(() => connectSSE(id), 5000);
  };
}

function appendEvent(container, data, cls = '') {
  if (!container) return;
  const div = document.createElement('div');
  div.className = `event ${cls}`;
  const type = data.event_type || data.type || 'event';
  const payload = data.payload || data;
  div.innerHTML = `<span class="event-time">${new Date().toLocaleTimeString()}</span>
    <strong>${type}</strong> ${typeof payload === 'string' ? payload : JSON.stringify(payload).slice(0, 200)}`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function updateTestPanel(data) {
  const panel = document.getElementById('tests-panel');
  if (!panel) return;
  panel.innerHTML = `
    <div class="test-summary">
      <span class="pass">${data.passed || 0} passed</span>
      <span class="fail">${data.failed || 0} failed</span>
      <span class="skip">${data.skipped || 0} skipped</span>
    </div>`;
}

function updateConfidencePanel(c) {
  const cp = document.getElementById('confidence-panel');
  if (!cp) return;
  cp.classList.remove('collapsed');
  const scoreColor = c.confidence_score >= 70 ? 'var(--test-pass)' : c.confidence_score >= 40 ? 'var(--status-ci)' : 'var(--test-fail)';
  cp.innerHTML = `
    <div style="font-size:13px">
      <div style="display:flex;align-items:center;gap:16px;margin-bottom:8px">
        <div style="font-size:24px;font-weight:bold;color:${scoreColor}">${c.confidence_score}/100</div>
        <div>
          <div>Files changed: <strong>${c.files_changed}</strong></div>
          <div>Lines: <span class="pass">+${c.lines_added}</span> / <span class="fail">-${c.lines_removed}</span></div>
        </div>
      </div>
      ${(c.changed_files || []).length ? `<div style="margin-top:6px"><strong>Changed:</strong> ${c.changed_files.map(f => '<code style="font-size:11px;background:var(--bg-card);padding:2px 6px;border-radius:4px">' + escHtml(f) + '</code>').join(' ')}</div>` : ''}
      ${(c.new_dependencies || []).length ? `<div style="margin-top:4px;color:var(--status-ci)"><strong>New deps:</strong> ${c.new_dependencies.map(d => escHtml(d)).join(', ')}</div>` : ''}
      ${(c.risk_flags || []).length ? `<div style="margin-top:4px;color:var(--test-fail)"><strong>Risks:</strong> ${c.risk_flags.map(r => escHtml(r)).join(' · ')}</div>` : ''}
    </div>`;
}

function updatePRPanel(prUrl) {
  const pp = document.getElementById('pr-panel');
  if (!pp) return;
  pp.classList.remove('collapsed');
  pp.innerHTML = `<a href="${prUrl}" target="_blank" style="color:var(--accent)">${prUrl}</a>`;
}

// --- Trace ---

async function loadTrace(id) {
  try {
    const trace = await api(`/sessions/${id}/trace`);
    renderTrace(trace.root_spans);
  } catch {}
}

function renderTrace(spans, depth = 0) {
  const container = document.getElementById('trace-timeline');
  if (!container) return;
  if (depth === 0) container.innerHTML = '';
  spans.forEach(s => {
    const dur = s.duration_ms ? `${(s.duration_ms / 1000).toFixed(1)}s` : '...';
    const bar = document.createElement('div');
    bar.className = 'span-row';
    bar.style.paddingLeft = `${depth * 20}px`;
    bar.innerHTML = `
      <span class="span-name">${s.name}</span>
      <span class="span-duration">${dur}</span>
      <span class="badge status-${s.status}" style="font-size:10px">${s.status}</span>`;
    container.appendChild(bar);
    if (s.children && s.children.length) renderTrace(s.children, depth + 1);
  });
}

// --- Triage ---

async function loadTriage(eligibility) {
  const params = eligibility && eligibility !== 'all' ? `?eligibility=${eligibility}` : '';
  const items = await api(`/triage${params}`);
  const list = document.getElementById('triage-list');
  if (!list) return;
  list.innerHTML = items.map(t => `
    <div class="card">
      <div class="card-header">
        <span class="card-title">#${t.issue_number} ${escHtml(t.issue_title)}</span>
        <span class="badge status-${t.eligibility === 'auto_assign' ? 'completed' : t.eligibility === 'needs_review' ? 'needs_human' : 'failed'}">${t.eligibility}</span>
      </div>
      <div class="card-meta">
        <span class="badge" style="background:var(--span-${t.issue_type === 'bug' ? 'container' : 'agent'})">${t.issue_type}</span>
        <span class="badge" style="background:${t.complexity === 'simple' ? 'var(--test-pass)' : t.complexity === 'medium' ? 'var(--status-ci)' : 'var(--status-failed)'}">${t.complexity}</span>
        ${t.session_id ? `<a href="/static/session.html?id=${t.session_id}" class="badge" style="background:var(--accent)">View Session</a>` : ''}
        ${t.eligibility === 'needs_review' ? `<button onclick="approveTriage('${t.id}')" style="font-size:11px;padding:2px 8px">Approve & Start</button>` : ''}
      </div>
      <details style="margin-top:8px"><summary class="collapsible" style="color:var(--text-secondary);font-size:12px">Details</summary>
        <div style="padding:8px;font-size:12px">
          <p><strong>Approach:</strong> ${escHtml(t.suggested_approach)}</p>
          <p><strong>Files:</strong> ${t.relevant_files.map(f => `<code>${f}</code>`).join(', ') || 'None'}</p>
        </div>
      </details>
    </div>
  `).join('') || '<p style="color:var(--text-secondary)">No triage results</p>';
}

function filterTriage(elig, btn) {
  document.querySelectorAll('.filter-tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadTriage(elig);
}

async function approveTriage(id) {
  await api(`/triage/${id}/approve`, { method: 'POST' });
  loadTriage();
}

// --- Metrics ---

async function showMetrics() {
  const data = await api('/metrics/summary');
  const panel = document.getElementById('metrics-panel');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = `
    <div class="metric-cards">
      <div class="metric-card"><div class="metric-value">${data.total_sessions}</div><div class="metric-label">Total Sessions</div></div>
      <div class="metric-card"><div class="metric-value">${data.success_rate}%</div><div class="metric-label">Success Rate</div></div>
      <div class="metric-card"><div class="metric-value">${data.avg_iterations.toFixed(1)}</div><div class="metric-label">Avg Iterations</div></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Outcomes</div>
      <div style="display:flex;gap:16px">
        <span style="color:var(--test-pass)">Completed: ${data.completed}</span>
        <span style="color:var(--test-fail)">Failed: ${data.failed}</span>
        <span style="color:var(--text-secondary)">Cancelled: ${data.cancelled}</span>
      </div>
    </div>
    ${data.common_failure_reasons.length ? `
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Failure Reasons</div>
      ${data.common_failure_reasons.map(r => `<div>${r.reason}: ${r.count}</div>`).join('')}
    </div>` : ''}`;
}

// --- Panel toggle ---

function togglePanel(name) {
  const body = document.getElementById(`${name}-panel`);
  if (body) body.classList.toggle('collapsed');
}

// --- Workflow DAG Tracker ---
// Tracks progress by matching status_change event messages from the workflow.
// The workflow emits status_change events at key transitions with distinctive messages.

const DAG_PHASE_IDS = ['prepare', 'code', 'document', 'deploy'];
const DAG_PHASE_STEPS = {
  prepare:  ['fetch_issue', 'provision_sandbox', 'clone_repo', 'get_or_create_repo_profile'],
  code:     ['enhance_spec', 'run_vibe_code', 'run_tests', 'generate_confidence_summary', 'commit_and_push'],
  document: ['open_pr', 'document_changes', 'update_pr_body'],
  deploy:   ['ci_result', 'review_feedback', 'destroy_sandbox'],
};

// Message patterns → what DAG state they imply
// Each rule: { match: string|regex, phase: phaseId, completedPhases: [...], activeSteps: [...], completedSteps: [...] }
const DAG_MESSAGE_RULES = [
  // Phase 1: Prepare — "Starting workflow" message
  { match: /Starting workflow/i, setPhase: 'prepare', activeStep: 'fetch_issue' },
  // Phase 1 complete: "Sandbox ready" message
  { match: /Sandbox ready/i, completePhase: 'prepare', setPhase: 'code', activeStep: 'enhance_spec' },
  // Phase 2 progress: "Coding complete" message
  { match: /Coding complete/i, completeSteps: ['enhance_spec', 'run_vibe_code', 'run_tests'], activeStep: 'generate_confidence_summary' },
  // Phase 2→3: "PR #" or "PR created" message
  { match: /PR (#|created)/i, completePhase: 'code', setPhase: 'document', activeStep: 'open_pr', completeSteps: ['generate_confidence_summary', 'commit_and_push'] },
  // Phase 3→4: status goes to ci_monitoring
  { statusMatch: 'ci_monitoring', completePhase: 'document', setPhase: 'deploy', activeStep: 'ci_result' },
  // Deploy progress: CI passed
  { match: /CI passed/i, completeSteps: ['ci_result'], activeStep: 'review_feedback' },
  { statusMatch: 'pr_review', completeSteps: ['ci_result'], activeStep: 'review_feedback' },
  // Terminal states
  { statusMatch: 'completed', completeAllPhases: true },
  { statusMatch: 'failed', failCurrentPhase: true },
  { statusMatch: 'cancelled', failCurrentPhase: true },
];

let _dagCurrentPhase = null;

function dagSetPhase(phaseId, status) {
  const phaseEl = document.getElementById(`dag-${phaseId}`);
  if (!phaseEl) return;
  phaseEl.classList.remove('phase-active', 'phase-completed', 'phase-failed');
  const sub = document.getElementById(`dag-${phaseId}-status`);
  if (status === 'active') {
    phaseEl.classList.add('phase-active');
    if (sub) sub.textContent = 'Running...';
    _dagCurrentPhase = phaseId;
  } else if (status === 'completed') {
    phaseEl.classList.add('phase-completed');
    if (sub) sub.textContent = 'Done';
    // Mark all steps in phase as completed
    (DAG_PHASE_STEPS[phaseId] || []).forEach(s => dagSetStep(s, 'completed'));
  } else if (status === 'failed') {
    phaseEl.classList.add('phase-failed');
    if (sub) sub.textContent = 'Failed';
  }
}

function dagSetStep(stepId, status) {
  const el = document.querySelector(`.dag-step[data-step="${stepId}"]`);
  if (!el) return;
  el.classList.remove('step-active', 'step-completed', 'step-failed');
  if (status) el.classList.add(`step-${status}`);
}

function dagUpdateProgress() {
  const fill = document.getElementById('dag-progress-fill');
  if (!fill) return;
  let done = 0, active = 0;
  for (const id of DAG_PHASE_IDS) {
    const el = document.getElementById(`dag-${id}`);
    if (el && el.classList.contains('phase-completed')) done++;
    else if (el && el.classList.contains('phase-active')) active++;
  }
  fill.style.width = Math.min((done * 25) + (active * 12), 100) + '%';
}

function dagCompletePhasesBefore(phaseId) {
  const idx = DAG_PHASE_IDS.indexOf(phaseId);
  for (let i = 0; i < idx; i++) {
    const prev = DAG_PHASE_IDS[i];
    const el = document.getElementById(`dag-${prev}`);
    if (el && !el.classList.contains('phase-completed')) {
      dagSetPhase(prev, 'completed');
    }
  }
}

function dagHandleSSE(data) {
  const eventType = data.event_type || data.type || '';
  if (eventType !== 'status_change') return;

  const payload = data.payload || data;
  const msg = payload.message || '';
  const status = payload.status || '';

  for (const rule of DAG_MESSAGE_RULES) {
    // Check if rule matches
    let matched = false;
    if (rule.match && rule.match.test(msg)) matched = true;
    if (rule.statusMatch && status === rule.statusMatch) matched = true;
    if (!matched) continue;

    // Complete prior phases
    if (rule.completePhase) {
      dagSetPhase(rule.completePhase, 'completed');
    }

    // Set active phase
    if (rule.setPhase) {
      dagCompletePhasesBefore(rule.setPhase);
      dagSetPhase(rule.setPhase, 'active');
    }

    // Complete specific steps
    if (rule.completeSteps) {
      rule.completeSteps.forEach(s => dagSetStep(s, 'completed'));
    }

    // Clear old active, set new active step
    if (rule.activeStep) {
      document.querySelectorAll('.dag-step.step-active').forEach(el => el.classList.remove('step-active'));
      dagSetStep(rule.activeStep, 'active');
    }

    // Complete all phases (terminal success)
    if (rule.completeAllPhases) {
      DAG_PHASE_IDS.forEach(id => dagSetPhase(id, 'completed'));
    }

    // Fail current phase (terminal failure)
    if (rule.failCurrentPhase) {
      document.querySelectorAll('.dag-step.step-active').forEach(el => {
        el.classList.remove('step-active');
        el.classList.add('step-failed');
      });
      if (_dagCurrentPhase) dagSetPhase(_dagCurrentPhase, 'failed');
    }

    dagUpdateProgress();
    // Don't break — multiple rules can match (e.g. status + message)
  }
}

// Initialize DAG from replayed SSE events (handled automatically since
// all past status_change events replay through the same dagHandleSSE)

// --- Utils ---

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
