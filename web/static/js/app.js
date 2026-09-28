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
})();
