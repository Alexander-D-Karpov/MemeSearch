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

  const B83 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~';
  const d83 = (str) => [...str].reduce((v, c) => v * 83 + B83.indexOf(c), 0);
  const toLinear = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const toSrgb = (v) => { v = Math.min(1, Math.max(0, v)); return Math.round((v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055) * 255); };
  const signPow = (v, e) => Math.sign(v) * Math.abs(v) ** e;
  const blurCache = new Map();
  const blurhashURL = (hash) => {
    if (blurCache.has(hash)) return blurCache.get(hash);
    let url = '';
    try {
      const flag = d83(hash[0]);
      const nx = (flag % 9) + 1;
      const ny = Math.floor(flag / 9) + 1;
      if (hash.length !== 4 + 2 * nx * ny) throw new Error('bad blurhash');
      const max = (d83(hash[1]) + 1) / 166;
      const colors = [];
      const dc = d83(hash.slice(2, 6));
      colors.push([toLinear(dc >> 16), toLinear((dc >> 8) & 255), toLinear(dc & 255)]);
      for (let i = 1; i < nx * ny; i++) {
        const v = d83(hash.slice(4 + i * 2, 6 + i * 2));
        colors.push([Math.floor(v / 361), Math.floor(v / 19) % 19, v % 19].map((q) => signPow((q - 9) / 9, 2) * max));
      }
      const w = 32;
      const h = 32;
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      const img = ctx.createImageData(w, h);
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
          let r = 0; let g = 0; let b = 0;
          for (let j = 0; j < ny; j++) {
            const cy = Math.cos((Math.PI * y * j) / h);
            for (let i = 0; i < nx; i++) {
              const basis = Math.cos((Math.PI * x * i) / w) * cy;
              const c = colors[i + j * nx];
              r += c[0] * basis; g += c[1] * basis; b += c[2] * basis;
            }
          }
          const o = 4 * (x + y * w);
          img.data[o] = toSrgb(r); img.data[o + 1] = toSrgb(g); img.data[o + 2] = toSrgb(b); img.data[o + 3] = 255;
        }
      }
      ctx.putImageData(img, 0, 0);
      url = canvas.toDataURL();
    } catch { }
    blurCache.set(hash, url);
    return url;
  };
  const reveal = (img) => {
    if (img.dataset.fade) return;
    img.dataset.fade = '1';
    if (img.complete && img.naturalWidth) return;
    img.classList.add('fade');
    img.addEventListener('load', () => img.classList.add('loaded'), { once: true });
  };
  const paintBlur = () => {
    for (const el of document.querySelectorAll('[data-bh]:not([data-bh-done])')) {
      el.dataset.bhDone = '1';
      const url = blurhashURL(el.dataset.bh);
      if (url) el.style.backgroundImage = `url(${url})`;
      el.querySelectorAll('img').forEach(reveal);
    }
  };
  paintBlur();
  let blurQueued = false;
  new MutationObserver(() => {
    if (blurQueued) return;
    blurQueued = true;
    requestAnimationFrame(() => { blurQueued = false; paintBlur(); });
  }).observe(document.body, { childList: true, subtree: true });

  const viewer = $('.viewer[data-bh]');
  if (viewer) {
    const clear = () => { viewer.style.backgroundImage = 'none'; };
    const media = viewer.querySelector('img, video');
    if (media instanceof HTMLVideoElement) {
      if (media.readyState >= 2) clear();
      else media.addEventListener('loadeddata', clear, { once: true });
      if (media.poster) {
        const poster = new Image();
        poster.onload = clear;
        poster.src = media.poster;
      }
    } else if (media) {
      if (media.complete && media.naturalWidth) clear();
      else media.addEventListener('load', clear, { once: true });
    }
  }

  document.querySelectorAll('.viewer img[data-full]').forEach((img) => {
    const full = new Image();
    full.decoding = 'async';
    full.src = img.dataset.full;
    const swap = () => { img.src = full.src; img.removeAttribute('data-full'); };
    if (full.decode) full.decode().then(swap, () => full.complete && full.naturalWidth && swap());
    else full.onload = swap;
  });

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
