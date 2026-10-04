(() => {
  const $ = (s, root = document) => root.querySelector(s);

  window.toast = (msg, ms = 2500) => {
    const t = $('#toast');
    if (!t) return;
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, ms);
  };

  document.addEventListener('error', (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement) || !img.closest('.card-media, .viewer')) return;
    const box = document.createElement('span');
    box.className = 'noimg';
    box.textContent = 'Preview unavailable';
    img.replaceWith(box);
  }, true);

  const ROW = 4;
  const sizer = new ResizeObserver((entries) => {
    for (const { target } of entries) {
      const gap = parseFloat(getComputedStyle(target).marginBottom) || 0;
      target.style.gridRowEnd = `span ${Math.max(1, Math.ceil((target.getBoundingClientRect().height + gap) / ROW))}`;
    }
  });
  const track = (grid) => {
    for (const el of grid.children) {
      if (!el.classList.contains('more') && !el.classList.contains('empty')) sizer.observe(el);
    }
  };
  document.querySelectorAll('.grid').forEach((grid) => {
    track(grid);
    new MutationObserver(() => track(grid)).observe(grid, { childList: true });
  });

  const results = $('#results');
  const meta = $('#meta');
  const input = $('#q');
  const kind = $('#kind');

  let observer;
  let loading = false;

  const watchMore = () => {
    if (!results) return;
    observer?.disconnect();
    const sentinel = results.querySelector('.more');
    if (!sentinel) return;
    observer = new IntersectionObserver(async (entries) => {
      if (!entries.some((e) => e.isIntersecting) || loading) return;
      loading = true;
      observer.disconnect();
      try {
        const res = await fetch(sentinel.dataset.next);
        const html = await res.text();
        sentinel.remove();
        results.insertAdjacentHTML('beforeend', html);
      } finally {
        loading = false;
        watchMore();
      }
    }, { rootMargin: '800px' });
    observer.observe(sentinel);
  };

  let controller;
  const liveSearch = async () => {
    if (!results) {
      input.form.submit();
      return;
    }
    const q = input.value.trim();
    const k = kind.value;
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (k) params.set('kind', k);
    const qs = params.toString();
    history.replaceState(null, '', qs ? `/?${qs}` : '/');
    document.title = q ? `${q} — Meme Search` : 'Meme Search';
    controller?.abort();
    controller = new AbortController();
    try {
      const res = await fetch(`/partials/results?${qs}`, { signal: controller.signal });
      const html = await res.text();
      results.innerHTML = html;
      const total = res.headers.get('X-Total');
      const took = res.headers.get('X-Took-Ms');
      if (meta) meta.textContent = q ? `${total} results for “${q}” · ${took} ms` : 'Latest memes';
      watchMore();
    } catch (e) {
      if (e.name !== 'AbortError') console.error(e);
    }
  };

  if (input) {
    let timer;
    if (results) {
      input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(liveSearch, 250);
      });
    }
    input.form.addEventListener('submit', (e) => {
      if (!results) return;
      e.preventDefault();
      clearTimeout(timer);
      liveSearch();
    });
    kind?.addEventListener('change', liveSearch);
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && document.activeElement !== input && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
        e.preventDefault();
        input.focus();
        input.select();
      }
    });
  }
  watchMore();

  document.addEventListener('click', async (e) => {
    const copy = e.target.closest('[data-copy],[data-copy-page]');
    if (!copy) return;
    const value = copy.hasAttribute('data-copy-page')
      ? location.href
      : new URL(copy.dataset.copy, location.href).href;
    try {
      await navigator.clipboard.writeText(value);
      toast('Link copied');
    } catch {
      prompt('Copy link', value);
    }
  });

  const imgForm = $('#img-search');
  const imgFile = $('#img-search-file');
  if (imgForm && imgFile) {
    const send = (file) => {
      if (!file || !file.type.startsWith('image/')) { window.toast('That is not a picture'); return; }
      if (file.size > 20 * 1024 * 1024) { window.toast('The picture is larger than 20 MB'); return; }
      const dt = new DataTransfer();
      dt.items.add(file);
      imgFile.files = dt.files;
      window.toast('Searching by picture…', 10000);
      imgForm.submit();
    };
    $('#img-search-btn')?.addEventListener('click', () => imgFile.click());
    imgFile.addEventListener('change', () => { if (imgFile.files[0]) send(imgFile.files[0]); });
    document.addEventListener('paste', (e) => {
      if (document.body.classList.contains('is-admin') && location.pathname.startsWith('/admin')) return;
      const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'));
      if (!item) return;
      e.preventDefault();
      send(item.getAsFile());
    });
    const overlay = $('#drop-overlay');
    let depth = 0;
    const hasFile = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
    const dropAllowed = () => !location.pathname.startsWith('/admin');
    document.addEventListener('dragenter', (e) => {
      if (!hasFile(e) || !dropAllowed()) return;
      depth += 1;
      overlay.hidden = false;
    });
    document.addEventListener('dragleave', () => {
      depth = Math.max(0, depth - 1);
      if (!depth) overlay.hidden = true;
    });
    document.addEventListener('dragover', (e) => { if (hasFile(e) && dropAllowed()) e.preventDefault(); });
    document.addEventListener('drop', (e) => {
      if (!hasFile(e) || !dropAllowed()) return;
      e.preventDefault();
      depth = 0;
      overlay.hidden = true;
      send(e.dataTransfer.files[0]);
    });
  }
})();
