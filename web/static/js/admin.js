(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];

  const api = async (method, url, body) => {
    const opts = { method, headers: {} };
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = typeof body === 'string' ? body : JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch { }
    if (!res.ok) throw new Error(data.error || data.detail || `${res.status} ${res.statusText}`);
    return data;
  };

  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-api]');
    if (!btn) return;
    e.preventDefault();
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    const [method, url] = btn.dataset.api.split(' ');
    btn.disabled = true;
    try {
      const data = await api(method, url, btn.dataset.body);
      if (data.queued !== undefined) toast(`Queued: ${data.queued === true ? 1 : data.queued}`);
      else if (data.job_id) toast(`Import #${data.job_id} started`);
      else toast('Done');
      if (btn.dataset.redirect) location.href = btn.dataset.redirect;
      else if (btn.hasAttribute('data-reload')) setTimeout(() => location.reload(), 400);
    } catch (err) {
      toast(err.message, 5000);
    } finally {
      btn.disabled = false;
    }
  });

  const MEDIA_EXT = /\.(jpe?g|png|gif|webp|bmp|heic|heif|avif|mp4|m4v|mov|webm|mkv|avi)$/i;
  const isMedia = (f) => MEDIA_EXT.test(f.name) || /^(image|video)\//.test(f.type);

  const readEntries = (reader) => new Promise((resolve, reject) => reader.readEntries(resolve, reject));
  const entryFile = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));

  const walk = async (entry, out) => {
    if (entry.isFile) {
      out.push(await entryFile(entry));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await readEntries(reader);
        if (!batch.length) break;
        for (const child of batch) await walk(child, out);
      }
    }
  };

  const dropzone = $('#dropzone');
  if (dropzone) {
    const log = $('#upload-log');
    const bar = $('#upload-bar');
    const summary = $('#upload-summary');
    const progress = $('#upload-progress');
    const CONCURRENCY = 4;
    const BATCH_FILES = 10;
    const BATCH_BYTES = 64 * 1024 * 1024;

    let state = null;

    const render = () => {
      if (!state) return;
      const pct = state.totalBytes ? Math.round((state.sentBytes / state.totalBytes) * 100) : 0;
      bar.style.width = `${pct}%`;
      summary.textContent = `${state.done}/${state.total} files · ${state.added} added · ${state.dups} duplicates · ${state.errors} errors · ${pct}%`;
    };

    const logLine = (text, cls) => {
      const li = document.createElement('li');
      li.textContent = text;
      if (cls) li.className = cls;
      log.prepend(li);
    };

    const sendBatch = (batch) => new Promise((resolve) => {
      const fd = new FormData();
      for (const f of batch) fd.append('files', f, f.webkitRelativePath || f.name);
      const xhr = new XMLHttpRequest();
      state.xhrs.add(xhr);
      const bulk = $('#bulk-priority').checked ? '?bulk=1' : '';
      xhr.open('POST', `/api/v1/admin/upload${bulk}`);
      let last = 0;
      xhr.upload.onprogress = (ev) => {
        state.sentBytes += ev.loaded - last;
        last = ev.loaded;
        render();
      };
      xhr.onloadend = () => {
        state.xhrs.delete(xhr);
        const batchBytes = batch.reduce((a, f) => a + f.size, 0);
        state.sentBytes += Math.max(0, batchBytes - last);
        state.done += batch.length;
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { }
        if (xhr.status === 200 && data) {
          for (const r of data.results) {
            if (r.error) { state.errors++; logLine(`✗ ${r.name}: ${r.error}`, 'error'); }
            else if (r.duplicate) state.dups++;
            else state.added++;
          }
        } else if (!state.cancelled) {
          state.errors += batch.length;
          logLine(`✗ batch of ${batch.length} failed: ${(data && data.error) || xhr.status}`, 'error');
        }
        render();
        resolve();
      };
      xhr.send(fd);
    });

    const upload = async (files) => {
      files = files.filter(isMedia);
      if (!files.length) { toast('No media files found'); return; }
      if (state && !state.finished) { toast('Upload already running'); return; }
      state = {
        total: files.length, done: 0, added: 0, dups: 0, errors: 0,
        totalBytes: files.reduce((a, f) => a + f.size, 0), sentBytes: 0,
        xhrs: new Set(), cancelled: false, finished: false,
      };
      progress.hidden = false;
      log.innerHTML = '';
      render();
      const batches = [];
      let cur = [], curBytes = 0;
      for (const f of files) {
        if (cur.length && (cur.length >= BATCH_FILES || curBytes + f.size > BATCH_BYTES)) {
          batches.push(cur);
          cur = []; curBytes = 0;
        }
        cur.push(f);
        curBytes += f.size;
      }
      if (cur.length) batches.push(cur);
      let next = 0;
      const worker = async () => {
        while (!state.cancelled && next < batches.length) await sendBatch(batches[next++]);
      };
      await Promise.all(Array.from({ length: CONCURRENCY }, worker));
      state.finished = true;
      logLine(state.cancelled ? 'Cancelled.' : `Finished: ${state.added} added, ${state.dups} duplicates, ${state.errors} errors.`);
      toast('Upload finished');
    };

    $('#upload-cancel').addEventListener('click', () => {
      if (!state) return;
      state.cancelled = true;
      state.xhrs.forEach((x) => x.abort());
    });
    $('#pick-files').addEventListener('change', (e) => upload([...e.target.files]));
    $('#pick-dir').addEventListener('change', (e) => upload([...e.target.files]));
    ['dragenter', 'dragover'].forEach((ev) => dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach((ev) => dropzone.addEventListener(ev, () => dropzone.classList.remove('over')));
    dropzone.addEventListener('drop', async (e) => {
      e.preventDefault();
      const items = [...(e.dataTransfer.items || [])];
      const files = [];
      const entries = items.map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
      if (entries.length) {
        for (const entry of entries) await walk(entry, files);
      } else {
        files.push(...e.dataTransfer.files);
      }
      upload(files);
    });
    window.addEventListener('beforeunload', (e) => {
      if (state && !state.finished) e.preventDefault();
    });
  }

  const zipForm = $('#zip-form');
  if (zipForm) {
    zipForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const file = zipForm.file.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file, file.name);
      const xhr = new XMLHttpRequest();
      const bar = $('#zip-bar');
      const summary = $('#zip-summary');
      $('#zip-progress').hidden = false;
      zipForm.querySelector('button').disabled = true;
      xhr.open('POST', '/api/v1/admin/import/zip');
      xhr.upload.onprogress = (ev) => {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        bar.style.width = `${pct}%`;
        summary.textContent = `Uploading ${pct}% (${(ev.loaded / 1048576).toFixed(0)} / ${(ev.total / 1048576).toFixed(0)} MB)`;
      };
      xhr.onloadend = () => {
        zipForm.querySelector('button').disabled = false;
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { }
        if (xhr.status === 200) {
          summary.textContent = `Uploaded. Import job #${data.job_id} is extracting on the server.`;
          pollImports(true);
        } else {
          summary.textContent = `Failed: ${data.error || xhr.status}`;
        }
      };
      xhr.send(fd);
    });
  }

  const importsTable = $('#imports');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let pollTimer;
  const pollImports = async (force) => {
    if (!importsTable) return;
    clearTimeout(pollTimer);
    try {
      const data = await api('GET', importsTable.dataset.poll);
      importsTable.tBodies[0].innerHTML = data.imports.map((j) => `<tr>
        <td>${j.id}</td><td>${esc(j.kind)}</td><td>${esc(j.name)}</td>
        <td class="status-${esc(j.status)}">${esc(j.status)}</td><td>${j.total}</td><td>${j.added}</td>
        <td>${j.duplicates}</td><td>${j.skipped}</td><td>${j.failed}</td><td class="error small">${esc(j.error)}</td></tr>`).join('');
      const active = data.imports.some((j) => j.status === 'pending' || j.status === 'running');
      if (active || force) pollTimer = setTimeout(() => pollImports(false), 3000);
    } catch { }
  };
  if (importsTable) pollImports(false);

  const selectAll = $('#select-all');
  if (selectAll) {
    const boxes = () => $$('.sel');
    const count = () => {
      const n = boxes().filter((b) => b.checked).length;
      $('#selected-count').textContent = `${n} selected`;
    };
    selectAll.addEventListener('change', () => {
      boxes().forEach((b) => { b.checked = selectAll.checked; });
      count();
    });
    document.addEventListener('change', (e) => { if (e.target.classList.contains('sel')) count(); });
    $$('[data-bulk]').forEach((btn) => btn.addEventListener('click', async () => {
      const ids = boxes().filter((b) => b.checked).map((b) => Number(b.value));
      if (!ids.length) { toast('Nothing selected'); return; }
      const action = btn.dataset.bulk;
      if (action === 'delete' && !confirm(`Delete ${ids.length} memes and their files?`)) return;
      try {
        const data = await api('POST', '/api/v1/admin/memes/bulk', { ids, action });
        toast(`${action}: ${data.affected}`);
        setTimeout(() => location.reload(), 500);
      } catch (err) { toast(err.message, 5000); }
    }));
  }

  const splitList = (s) => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

  const editForm = $('#edit-form');
  if (editForm) {
    const TEXT = { title: 'title', text: 'text', description: 'description', transcript: 'transcript', template: 'template' };
    const LISTS = ['tags', 'objects', 'people'];
    const FLAGS = ['nsfw', 'hidden', 'locked'];
    const initial = {};
    [...Object.keys(TEXT), ...LISTS].forEach((n) => { initial[n] = editForm[n].value; });
    FLAGS.forEach((n) => { initial[n] = editForm[n].checked; });
    editForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = editForm;
      const body = {};
      Object.entries(TEXT).forEach(([n, key]) => { if (f[n].value !== initial[n]) body[key] = f[n].value; });
      LISTS.forEach((n) => { if (f[n].value !== initial[n]) body[n] = splitList(f[n].value); });
      FLAGS.forEach((n) => { if (f[n].checked !== initial[n]) body[n] = f[n].checked; });
      if (!Object.keys(body).length) { location.href = `/m/${f.dataset.id}`; return; }
      try {
        await api('PATCH', `/api/v1/admin/memes/${f.dataset.id}`, body);
        location.href = `/m/${f.dataset.id}`;
      } catch (err) { toast(err.message, 5000); }
    });
  }

  const settingsForm = $('#settings-form');
  if (settingsForm) {
    settingsForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = settingsForm;
      try {
        await api('PUT', '/api/v1/admin/settings', {
          prompt_extra: f.prompt_extra.value,
          codex_enabled: f.codex_enabled.checked,
          fallback_enabled: f.fallback_enabled.checked,
          transcribe: f.transcribe.checked,
          codex_model: f.codex_model.value.trim(),
          codex_effort: f.codex_effort.value,
          fallback_model: f.fallback_model.value.trim(),
        });
        toast('Saved');
      } catch (err) { toast(err.message, 5000); }
    });
  }

  $$('time[data-local]').forEach((t) => {
    const d = new Date(t.dataset.local);
    if (!isNaN(d)) t.textContent = d.toLocaleString();
  });

  const renderUsage = (cell) => {
    let u = {};
    try { u = JSON.parse(cell.dataset.usage || '{}'); } catch { }
    const rl = u.rateLimits || u.rate_limits;
    if (!rl) { cell.innerHTML = '<p class="small muted">Not checked yet. Press Check account.</p>'; return; }
    const windowName = (w, fallback) => {
      const mins = w.windowDurationMins ?? w.window_duration_mins;
      if (!mins) return fallback;
      return mins >= 1440 ? `${Math.round(mins / 1440)}-day limit` : `${Math.round(mins / 60)}-hour limit`;
    };
    const win = (w, fallback) => {
      if (!w) return '';
      const used = w.usedPercent ?? w.used_percent ?? 0;
      const reset = w.resetsAt ?? w.resets_at;
      const when = reset ? new Date(reset * 1000).toLocaleString() : '';
      return `<div class="small">${esc(windowName(w, fallback))}: ${used}% used${when ? `, resets ${esc(when)}` : ''}</div><div class="meter"><div style="width:${Math.min(100, used)}%"></div></div>`;
    };
    cell.innerHTML = win(rl.primary, 'Short limit') + win(rl.secondary, 'Weekly limit') || '<p class="small muted">No limits reported.</p>';
  };
  $$('.usage[data-usage]').forEach(renderUsage);

  const codexAdd = $('#codex-add');
  if (codexAdd) {
    codexAdd.addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        await api('POST', '/api/v1/admin/codex', { name: codexAdd.name.value.trim() });
        location.reload();
      } catch (err) { toast(err.message, 5000); }
    });

    const loginPoll = async (row, box) => {
      const id = row.dataset.id;
      try {
        const s = await api('GET', `/api/v1/admin/codex/${id}/login`);
        if (s.state === 'pending') {
          box.hidden = false;
          box.innerHTML = `<div>1. Open <a href="${esc(s.verification_url)}" target="_blank" rel="noopener">${esc(s.verification_url)}</a></div>
            <div>2. Enter code <span class="code">${esc(s.user_code)}</span></div>
            <div class="muted small">Waiting for confirmation… <button class="btn" data-cancel-login>Cancel</button></div>`;
          setTimeout(() => loginPoll(row, box), 3000);
        } else if (s.state === 'done') {
          box.innerHTML = '<b class="status-ok">Logged in.</b>';
          setTimeout(() => location.reload(), 1200);
        } else if (s.state === 'failed') {
          box.innerHTML = `<span class="error">Login failed: ${esc(s.error)}</span>`;
        } else {
          box.hidden = true;
        }
      } catch (err) {
        box.innerHTML = `<span class="error">${esc(err.message)}</span>`;
      }
    };

    $$('.session[data-id]').forEach((row) => {
      const id = row.dataset.id;
      const box = row.querySelector('.login-box');
      api('GET', `/api/v1/admin/codex/${id}/login`).then((s) => { if (s.state === 'pending') loginPoll(row, box); }).catch(() => { });

      row.addEventListener('click', async (e) => {
        if (e.target.closest('[data-cancel-login]')) {
          await api('DELETE', `/api/v1/admin/codex/${id}/login`).catch(() => { });
          box.hidden = true;
          return;
        }
        const btn = e.target.closest('[data-codex]');
        if (!btn) return;
        const action = btn.dataset.codex;
        btn.disabled = true;
        try {
          if (action === 'login') {
            await api('POST', `/api/v1/admin/codex/${id}/login`);
            box.hidden = false;
            box.textContent = 'Starting device login…';
            loginPoll(row, box);
          } else if (action === 'check') {
            const r = await api('POST', `/api/v1/admin/codex/${id}/check`);
            toast(r.email ? `OK: ${r.email} (${r.plan || 'plan ?'})` : `Status: ${r.status}`, 4000);
            setTimeout(() => location.reload(), 800);
          } else if (action === 'clear') {
            await api('PATCH', `/api/v1/admin/codex/${id}`, { clear_cooldown: true });
            location.reload();
          } else if (action === 'logout') {
            if (!confirm('Log this session out of Codex?')) return;
            await api('POST', `/api/v1/admin/codex/${id}/logout`);
            location.reload();
          } else if (action === 'delete') {
            if (!confirm('Delete this session and its credentials?')) return;
            await api('DELETE', `/api/v1/admin/codex/${id}`);
            location.reload();
          }
        } catch (err) {
          toast(err.message, 6000);
        } finally {
          btn.disabled = false;
        }
      });

      row.querySelector('[data-codex-auth]').addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const fd = new FormData();
        fd.append('file', file);
        try {
          const r = await api('POST', `/api/v1/admin/codex/${id}/auth`, fd);
          toast(r.email ? `Imported: ${r.email}` : 'Imported', 4000);
          setTimeout(() => location.reload(), 800);
        } catch (err) { toast(err.message, 6000); }
        e.target.value = '';
      });

      row.querySelector('[data-toggle-enabled]').addEventListener('change', async (e) => {
        try { await api('PATCH', `/api/v1/admin/codex/${id}`, { enabled: e.target.checked }); toast('Saved'); } catch (err) { toast(err.message); }
      });
      row.querySelector('[data-priority]').addEventListener('change', async (e) => {
        try { await api('PATCH', `/api/v1/admin/codex/${id}`, { priority: Number(e.target.value) }); toast('Saved'); } catch (err) { toast(err.message); }
      });
    });
  }
})();
