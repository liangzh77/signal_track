from __future__ import annotations


def render_inbox_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signal Track Inbox</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0D0F0E;
      --surface: rgba(245,247,244,.07);
      --surface-raised: rgba(245,247,244,.105);
      --border: rgba(231,238,232,.16);
      --border-strong: rgba(231,238,232,.26);
      --text: #F1F5EF;
      --muted: #AEB9B0;
      --faint: #727D75;
      --cyan: #44D7C8;
      --amber: #D8B35D;
      --red: #FF6B6B;
      --green: #58D68D;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px),
        var(--bg);
      background-size: 32px 32px;
      color: var(--text);
      font-family: Geist, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shell { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .topbar { display: flex; justify-content: space-between; align-items: end; gap: 16px; padding: 18px 0 22px; }
    h1 { margin: 0; font-size: 28px; line-height: 36px; letter-spacing: 0; }
    .nav { display: flex; gap: 10px; flex-wrap: wrap; }
    a, button { color: inherit; }
    .nav a, .button {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      min-height: 36px; padding: 0 13px; border: 1px solid var(--border);
      border-radius: 999px; background: rgba(245,247,244,.05);
      text-decoration: none; cursor: pointer; font: inherit;
    }
    .button.primary { color: #071311; border-color: rgba(68,215,200,.72); background: var(--cyan); font-weight: 700; }
    .button.secondary:hover, .nav a:hover { color: var(--cyan); border-color: rgba(68,215,200,.55); }
    .grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(280px, .75fr); gap: 16px; align-items: start; }
    .card {
      border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
      box-shadow: 0 1px 0 rgba(255,255,255,.06) inset, 0 16px 48px rgba(0,0,0,.24);
      backdrop-filter: blur(18px);
    }
    .panel { padding: 16px; }
    h2 { margin: 0 0 14px; font-size: 18px; line-height: 26px; }
    .form { display: grid; gap: 12px; }
    label { display: grid; gap: 7px; color: var(--muted); font-size: 12px; line-height: 18px; }
    input, textarea, select {
      width: 100%; border: 1px solid var(--border); border-radius: 8px;
      background: rgba(0,0,0,.22); color: var(--text); font: inherit;
      padding: 10px 11px; outline: none;
    }
    textarea { min-height: 280px; resize: vertical; line-height: 1.55; }
    textarea.compact { min-height: 96px; }
    input:focus, textarea:focus, select:focus { border-color: rgba(68,215,200,.65); box-shadow: 0 0 0 3px rgba(68,215,200,.08); }
    .row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .inline { display: flex; align-items: center; gap: 9px; color: var(--muted); min-height: 38px; }
    .inline input { width: 18px; height: 18px; accent-color: var(--cyan); }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .drop {
      border: 1px dashed var(--border-strong); border-radius: 8px; padding: 18px;
      min-height: 132px; display: grid; place-items: center; background: rgba(255,255,255,.025);
    }
    .drop.active { border-color: rgba(68,215,200,.75); background: rgba(68,215,200,.08); }
    .drop input { border: 0; padding: 0; background: transparent; }
    .status { min-height: 24px; color: var(--muted); font-size: 13px; line-height: 20px; }
    .status.ok { color: var(--green); }
    .status.warn { color: var(--amber); }
    .status.error { color: var(--red); }
    pre {
      margin: 0; max-height: 520px; overflow: auto; white-space: pre-wrap; word-break: break-word;
      border: 1px solid rgba(231,238,232,.1); border-radius: 8px; padding: 12px;
      background: rgba(0,0,0,.22); color: var(--muted); font: 12px/18px "IBM Plex Mono", "Geist Mono", monospace;
    }
    .result-grid { display: grid; gap: 10px; }
    .mini { display: grid; gap: 8px; margin-top: 10px; }
    .mini a { color: var(--cyan); text-decoration: none; }
    .feed { display: grid; gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid rgba(231,238,232,.1); }
    .feed-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
    .feed-head h3 { margin: 0; font-size: 15px; line-height: 22px; }
    .feed-head button { min-height: 30px; padding: 0 10px; }
    .input-feed { display: grid; gap: 8px; }
    .input-item { border: 1px solid rgba(231,238,232,.08); border-radius: 8px; padding: 10px; background: rgba(255,255,255,.018); }
    .input-top { display: flex; justify-content: space-between; gap: 8px; align-items: start; }
    .input-action { border: 1px solid var(--border-strong); border-radius: 999px; padding: 2px 8px; font-size: 11px; line-height: 16px; color: var(--muted); }
    .input-action.close, .input-action.exit_signal { color: var(--red); border-color: rgba(255,107,107,.58); background: rgba(255,107,107,.08); }
    .input-action.update { color: var(--amber); border-color: rgba(216,179,93,.58); background: rgba(216,179,93,.08); }
    .input-action.track { color: var(--cyan); border-color: rgba(68,215,200,.55); background: rgba(68,215,200,.07); }
    .input-preview { color: var(--muted); font-size: 12px; line-height: 18px; margin-top: 6px; }
    .input-meta { color: var(--faint); font-size: 11px; line-height: 16px; margin-top: 6px; }
    @media (max-width: 820px) {
      .shell { padding: 16px; }
      .topbar { align-items: start; flex-direction: column; }
      .grid, .row { grid-template-columns: 1fr; }
      textarea { min-height: 220px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div>
        <h1>Signal Track Inbox</h1>
        <div class="status">Ready</div>
      </div>
      <nav class="nav">
        <a href="/dashboard">Dashboard</a>
        <a href="/api/projects">Projects JSON</a>
      </nav>
    </section>

    <section class="grid">
      <article class="card panel">
        <h2>New Input</h2>
        <form id="text-form" class="form">
          <div class="row">
            <label>Source
              <input id="source" name="source" autocomplete="off" placeholder="Alpha Desk">
            </label>
            <label>Extractor
              <select id="extractor" name="extractor">
                <option value="auto">auto</option>
                <option value="heuristic">heuristic</option>
                <option value="openai">openai</option>
              </select>
            </label>
          </div>
          <label>Content
            <textarea id="content" name="content" placeholder="00700.HK long, watch ads recovery."></textarea>
          </label>
          <div class="row">
            <label>API Key
              <input id="api-key" name="api-key" type="password" autocomplete="off">
            </label>
            <label class="inline"><input id="portfolio" name="portfolio" type="checkbox"> Portfolio</label>
          </div>
          <div class="actions">
            <button class="button primary" type="submit">Submit Text</button>
            <button class="button secondary" type="button" id="clear-result">Clear Result</button>
          </div>
        </form>
      </article>

      <aside class="card panel">
        <h2>File Upload</h2>
        <form id="file-form" class="form">
          <div class="drop" id="drop-zone">
            <label>File
              <input id="file" name="file" type="file">
            </label>
          </div>
          <div class="actions">
            <button class="button primary" type="submit">Upload File</button>
          </div>
        </form>
        <div class="mini">
          <a href="/api/inputs">Recent inputs</a>
          <a href="/api/exit-signals">Exit signals</a>
          <a href="/health">Health</a>
        </div>
        <div class="feed">
          <div class="feed-head">
            <h3>Recent Inputs</h3>
            <button class="button secondary" type="button" id="refresh-inputs">Refresh</button>
          </div>
          <div id="recent-inputs" class="input-feed"></div>
        </div>
      </aside>

      <article class="card panel">
        <h2>Project Update</h2>
        <div class="form">
          <div class="row">
            <label>Project ID
              <input id="project-id" autocomplete="off" inputmode="numeric" placeholder="1">
            </label>
            <label>Project
              <select id="project-select">
                <option value="">Load projects</option>
              </select>
            </label>
          </div>
          <div class="row">
            <label>Note Type
              <select id="note-type">
                <option value="source_update">source_update</option>
                <option value="manual_note">manual_note</option>
                <option value="system_logic">system_logic</option>
              </select>
            </label>
            <label>Provider
              <select id="project-note-provider">
                <option value="none">none</option>
                <option value="fixture">fixture</option>
                <option value="auto">auto</option>
                <option value="tushare">tushare</option>
                <option value="yfinance">yfinance</option>
              </select>
            </label>
          </div>
          <div class="row">
            <label class="inline"><input id="auto-refresh-projects" type="checkbox" checked> Auto refresh</label>
            <label class="inline"><input id="project-note-run-check" type="checkbox"> Run check after note</label>
          </div>
          <label>Observation
            <textarea id="project-note" class="compact" placeholder="manual observation: ads recovered"></textarea>
          </label>
          <div class="actions">
            <button class="button primary" type="button" id="submit-note">Add Note</button>
            <button class="button secondary" type="button" id="refresh-projects">Refresh Projects</button>
          </div>
          <label>Weights JSON
            <textarea id="weights-json" class="compact" placeholder='{"300750.SZ": 60, "600519.SH": 40}'></textarea>
          </label>
          <div class="actions">
            <button class="button secondary" type="button" id="submit-weights">Update Weights</button>
          </div>
          <div class="row">
            <label>Close Date
              <input id="close-date" type="date">
            </label>
            <label>Close Reason
              <input id="close-reason" autocomplete="off" placeholder="manual exit after thesis broke">
            </label>
          </div>
          <div class="actions">
            <button class="button secondary" type="button" id="submit-close">Close Project</button>
          </div>
        </div>
      </article>

      <article class="card panel">
        <h2>Research Verification</h2>
        <div class="form">
          <label>Item
            <select id="research-item-select">
              <option value="">Load research items</option>
            </select>
          </label>
          <div class="row">
            <label>Status
              <select id="research-status">
                <option value="verified">verified</option>
                <option value="contradicted">contradicted</option>
                <option value="pending">pending</option>
                <option value="unverified">unverified</option>
                <option value="ignored">ignored</option>
              </select>
            </label>
            <label>Provider
              <select id="research-provider">
                <option value="none">none</option>
                <option value="fixture">fixture</option>
                <option value="auto">auto</option>
                <option value="tushare">tushare</option>
                <option value="yfinance">yfinance</option>
              </select>
            </label>
          </div>
          <label>Source Note
            <textarea id="research-source-note" class="compact" placeholder="checked filing / source URL / manual verification"></textarea>
          </label>
          <label class="inline"><input id="research-run-check" type="checkbox"> Run check after update</label>
          <div class="actions">
            <button class="button primary" type="button" id="submit-research">Update Research Item</button>
            <button class="button secondary" type="button" id="refresh-research">Refresh Items</button>
          </div>
        </div>
      </article>

      <article class="card panel">
        <h2>Daily Operations</h2>
        <div class="form">
          <div class="row">
            <label>Provider
              <select id="check-provider">
                <option value="">default</option>
                <option value="none">none</option>
                <option value="fixture">fixture</option>
                <option value="auto">auto</option>
                <option value="tushare">tushare</option>
                <option value="yfinance">yfinance</option>
              </select>
            </label>
            <label>Check Date
              <input id="check-date" type="date">
            </label>
          </div>
          <div class="actions">
            <button class="button primary" type="button" id="run-checks">Run Check</button>
            <button class="button secondary" type="button" id="publish-dashboard">Publish Dashboard</button>
            <button class="button secondary" type="button" id="refresh-health">Refresh Health</button>
          </div>
        </div>
      </article>

      <article class="card panel">
        <h2>Market Data</h2>
        <div class="form">
          <div class="row">
            <label>Provider
              <select id="market-provider">
                <option value="auto">auto</option>
                <option value="fixture">fixture</option>
                <option value="tushare">tushare</option>
                <option value="yfinance">yfinance</option>
              </select>
            </label>
            <label>Market
              <select id="market-name">
                <option value="all">all</option>
                <option value="CN_A">CN_A</option>
                <option value="HK">HK</option>
                <option value="CN_FUT">CN_FUT</option>
                <option value="HK_FUT">HK_FUT</option>
                <option value="US">US</option>
                <option value="US_FUT">US_FUT</option>
              </select>
            </label>
          </div>
          <div class="row">
            <label>Smoke Days
              <input id="market-smoke-days" type="number" min="1" max="365" value="30">
            </label>
            <label>Sample Size
              <input id="market-sample-size" type="number" min="1" max="5" value="1">
            </label>
          </div>
          <div class="actions">
            <button class="button primary" type="button" id="market-coverage">Coverage</button>
            <button class="button secondary" type="button" id="market-smoke">Smoke</button>
            <button class="button secondary" type="button" id="refresh-instruments">Refresh Master</button>
          </div>
        </div>
      </article>

      <article class="card panel">
        <h2>Result</h2>
        <div id="status" class="status"></div>
        <pre id="result">{}</pre>
      </article>
    </section>
  </main>
  <script>
    const apiKeyInput = document.getElementById('api-key');
    const sourceInput = document.getElementById('source');
    const extractorInput = document.getElementById('extractor');
    const portfolioInput = document.getElementById('portfolio');
    const contentInput = document.getElementById('content');
    const statusNode = document.getElementById('status');
    const resultNode = document.getElementById('result');
    const fileInput = document.getElementById('file');
    const dropZone = document.getElementById('drop-zone');
    const projectIdInput = document.getElementById('project-id');
    const projectSelectInput = document.getElementById('project-select');
    const autoRefreshProjectsInput = document.getElementById('auto-refresh-projects');
    const noteTypeInput = document.getElementById('note-type');
    const projectNoteProviderInput = document.getElementById('project-note-provider');
    const projectNoteRunCheckInput = document.getElementById('project-note-run-check');
    const projectNoteInput = document.getElementById('project-note');
    const weightsJsonInput = document.getElementById('weights-json');
    const closeDateInput = document.getElementById('close-date');
    const closeReasonInput = document.getElementById('close-reason');
    const researchItemSelectInput = document.getElementById('research-item-select');
    const researchStatusInput = document.getElementById('research-status');
    const researchProviderInput = document.getElementById('research-provider');
    const researchSourceNoteInput = document.getElementById('research-source-note');
    const researchRunCheckInput = document.getElementById('research-run-check');
    const checkProviderInput = document.getElementById('check-provider');
    const checkDateInput = document.getElementById('check-date');
    const recentInputsNode = document.getElementById('recent-inputs');
    const marketProviderInput = document.getElementById('market-provider');
    const marketNameInput = document.getElementById('market-name');
    const marketSmokeDaysInput = document.getElementById('market-smoke-days');
    const marketSampleSizeInput = document.getElementById('market-sample-size');

    apiKeyInput.value = localStorage.getItem('signalTrackApiKey') || '';
    apiKeyInput.addEventListener('input', () => localStorage.setItem('signalTrackApiKey', apiKeyInput.value));

    function headers(extra = {}) {
      const key = apiKeyInput.value.trim();
      return key ? { ...extra, Authorization: `Bearer ${key}` } : extra;
    }

    function show(payload, ok) {
      statusNode.className = ok ? 'status ok' : 'status error';
      statusNode.textContent = ok ? 'Saved' : 'Failed';
      resultNode.textContent = JSON.stringify(payload, null, 2);
      if (ok && autoRefreshProjectsInput.checked) {
        loadProjects();
        loadResearchItems();
        loadInputs();
      }
    }

    async function parseResponse(response) {
      const text = await response.text();
      try { return JSON.parse(text); } catch { return { body: text }; }
    }

    document.getElementById('text-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      statusNode.className = 'status warn';
      statusNode.textContent = 'Submitting...';
      const response = await fetch('/api/inputs', {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          source: sourceInput.value || null,
          content: contentInput.value,
          portfolio: portfolioInput.checked,
          extractor: extractorInput.value,
        }),
      });
      show(await parseResponse(response), response.ok);
    });

    document.getElementById('file-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!fileInput.files.length) {
        statusNode.className = 'status warn';
        statusNode.textContent = 'Choose a file first';
        return;
      }
      statusNode.className = 'status warn';
      statusNode.textContent = 'Uploading...';
      const data = new FormData();
      data.append('source', sourceInput.value || '');
      data.append('portfolio', portfolioInput.checked ? 'true' : 'false');
      data.append('extractor', extractorInput.value);
      data.append('file', fileInput.files[0]);
      const response = await fetch('/api/inputs/file', { method: 'POST', headers: headers(), body: data });
      show(await parseResponse(response), response.ok);
    });

    document.getElementById('clear-result').addEventListener('click', () => {
      statusNode.className = 'status';
      statusNode.textContent = '';
      resultNode.textContent = '{}';
    });

    async function loadProjects() {
      try {
        const response = await fetch('/api/projects');
        const projects = await parseResponse(response);
        if (!response.ok || !Array.isArray(projects)) return;
        const current = projectIdInput.value;
        projectSelectInput.innerHTML = '<option value="">Select project</option>';
        projects.forEach((project) => {
          const option = document.createElement('option');
          option.value = project.id;
          option.textContent = `#${project.id} ${project.source_name} · ${project.title} · ${project.status}`;
          projectSelectInput.appendChild(option);
        });
        if (current) projectSelectInput.value = current;
      } catch (error) {
        statusNode.className = 'status warn';
        statusNode.textContent = 'Project list unavailable';
      }
    }

    async function loadResearchItems() {
      try {
        const id = projectIdInput.value.trim();
        const path = id ? `/api/research-items?project_id=${encodeURIComponent(id)}` : '/api/research-items';
        const response = await fetch(path);
        const items = await parseResponse(response);
        if (!response.ok || !Array.isArray(items)) return;
        const current = researchItemSelectInput.value;
        researchItemSelectInput.innerHTML = '<option value="">Select item</option>';
        items.forEach((item) => {
          const option = document.createElement('option');
          option.value = item.id;
          option.textContent = `#${item.id} [${item.status}] ${item.item_type} · ${item.content}`;
          researchItemSelectInput.appendChild(option);
        });
        if (current) researchItemSelectInput.value = current;
      } catch (error) {
        statusNode.className = 'status warn';
        statusNode.textContent = 'Research items unavailable';
      }
    }

    async function loadInputs() {
      try {
        const response = await fetch('/api/inputs?limit=8');
        const inputs = await parseResponse(response);
        if (!response.ok || !Array.isArray(inputs)) return;
        recentInputsNode.innerHTML = inputs.length
          ? inputs.map(renderInputItem).join('')
          : '<div class="input-item"><div class="input-preview">No inputs yet</div></div>';
      } catch (error) {
        recentInputsNode.innerHTML = '<div class="input-item"><div class="input-preview">Inputs unavailable</div></div>';
      }
    }

    function renderInputItem(item) {
      const action = item.input_action || 'none';
      const symbols = (item.resolved_symbols || []).join(', ') || '--';
      const count = (item.project_ids || []).length;
      return `
        <div class="input-item" data-input-action="${escapeHtml(action)}">
          <div class="input-top">
            <strong>${escapeHtml(item.source_name || '')}</strong>
            <span class="input-action ${escapeHtml(action)}">${escapeHtml(action)}</span>
          </div>
          <div class="input-preview">${escapeHtml(item.content_preview || '')}</div>
          <div class="input-meta">${escapeHtml(item.received_at || '')} · ${escapeHtml(symbols)} · projects ${count}</div>
        </div>`;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    projectSelectInput.addEventListener('change', () => {
      projectIdInput.value = projectSelectInput.value;
      loadResearchItems();
    });

    document.getElementById('refresh-projects').addEventListener('click', loadProjects);
    document.getElementById('refresh-research').addEventListener('click', loadResearchItems);
    document.getElementById('refresh-inputs').addEventListener('click', loadInputs);

    function projectId() {
      const id = projectIdInput.value.trim();
      if (!id) {
        show({ error: 'Project ID is required' }, false);
        return null;
      }
      return id;
    }

    async function runProjectAction(path, options) {
      try {
        statusNode.className = 'status warn';
        statusNode.textContent = 'Updating...';
        const response = await fetch(path, options);
        show(await parseResponse(response), response.ok);
      } catch (error) {
        show({ error: error.message }, false);
      }
    }

    document.getElementById('submit-note').addEventListener('click', async () => {
      const id = projectId();
      if (!id) return;
      await runProjectAction(`/api/projects/${id}/logic-blocks`, {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          logic_type: noteTypeInput.value,
          content: projectNoteInput.value,
          confidence: 1.0,
          run_check: projectNoteRunCheckInput.checked,
          provider: projectNoteProviderInput.value,
        }),
      });
    });

    document.getElementById('submit-weights').addEventListener('click', async () => {
      const id = projectId();
      if (!id) return;
      let weights;
      try { weights = JSON.parse(weightsJsonInput.value || '{}'); }
      catch (error) {
        show({ error: 'Weights JSON is invalid' }, false);
        return;
      }
      await runProjectAction(`/api/projects/${id}/weights`, {
        method: 'PATCH',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ weights }),
      });
    });

    document.getElementById('submit-close').addEventListener('click', async () => {
      const id = projectId();
      if (!id) return;
      await runProjectAction(`/api/projects/${id}/close`, {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          closed_date: closeDateInput.value || null,
          reason: closeReasonInput.value || null,
        }),
      });
    });

    document.getElementById('submit-research').addEventListener('click', async () => {
      const itemId = researchItemSelectInput.value;
      if (!itemId) {
        show({ error: 'Research item is required' }, false);
        return;
      }
      await runProjectAction(`/api/research-items/${itemId}`, {
        method: 'PATCH',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          status: researchStatusInput.value,
          source_note: researchSourceNoteInput.value || null,
          run_check: researchRunCheckInput.checked,
          provider: researchProviderInput.value,
        }),
      });
    });

    document.getElementById('run-checks').addEventListener('click', async () => {
      await runProjectAction('/api/checks/run', {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          provider: checkProviderInput.value || null,
          date: checkDateInput.value || null,
        }),
      });
    });

    document.getElementById('publish-dashboard').addEventListener('click', async () => {
      await runProjectAction('/api/publish', {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/json' }),
      });
    });

    document.getElementById('market-coverage').addEventListener('click', async () => {
      const provider = encodeURIComponent(marketProviderInput.value);
      await runProjectAction(`/api/market-data/coverage?provider=${provider}`, { method: 'GET' });
    });

    document.getElementById('market-smoke').addEventListener('click', async () => {
      const provider = encodeURIComponent(marketProviderInput.value);
      const market = encodeURIComponent(marketNameInput.value);
      const days = encodeURIComponent(marketSmokeDaysInput.value || '30');
      const sampleSize = encodeURIComponent(marketSampleSizeInput.value || '1');
      await runProjectAction(
        `/api/market-data/smoke?provider=${provider}&market=${market}&days=${days}&sample_size=${sampleSize}`,
        { method: 'GET' },
      );
    });

    document.getElementById('refresh-instruments').addEventListener('click', async () => {
      await runProjectAction('/api/instruments/refresh', {
        method: 'POST',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          provider: marketProviderInput.value,
          market: marketNameInput.value,
        }),
      });
    });

    document.getElementById('refresh-health').addEventListener('click', async () => {
      try {
        statusNode.className = 'status warn';
        statusNode.textContent = 'Checking health...';
        const response = await fetch('/health');
        show(await parseResponse(response), response.ok);
      } catch (error) {
        show({ error: error.message }, false);
      }
    });

    ['dragenter', 'dragover'].forEach((name) => dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      dropZone.classList.add('active');
    }));
    ['dragleave', 'drop'].forEach((name) => dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      dropZone.classList.remove('active');
    }));
    dropZone.addEventListener('drop', (event) => {
      if (event.dataTransfer.files.length) fileInput.files = event.dataTransfer.files;
    });
    loadProjects();
    loadResearchItems();
    loadInputs();
  </script>
</body>
</html>"""
