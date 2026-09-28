# MemeSearch

Meme search engine for images, GIFs and videos. Codex (ChatGPT subscription) reads each meme: verbatim text, description, the joke, objects, people, template and tags. An OpenAI-compatible API is the fallback. Search is hybrid: full-text + fuzzy trigram + multilingual text vectors + SigLIP2 image vectors, fused with RRF.

## Services

| service | lang | role |
|---|---|---|
| `web` | Go | public search UI, meme pages, admin UI, JSON API, uploads, ZIP/inbox import |
| `ml` | Python / FastAPI | SigLIP2 + multilingual-e5 embeddings, Codex session management (device login, auth.json import) |
| `worker` | Python | queue consumer: frames/thumbs (ffmpeg), Whisper speech, Codex/fallback analysis, vectors |
| `bot` | Python / aiogram | forward media to the bot to add it |
| `postgres` | pgvector | memes, HNSW vector indexes, GIN trigram + tsvector |
| `redis` | | job streams, search/embedding cache, sessions, events |

Media lives in `MEDIA_HOST_DIR` (e.g. an external drive): `originals/aa/bb/<sha256>.<ext>`, `thumbs/aa/bb/<sha256>.webp`, `inbox/`. nginx serves `/media/originals` and `/media/thumbs` directly from it.

## Deploy (ms.akarpov.ru)

Needs Docker with the compose plugin, nginx and certbot on the host. The server CPU must be x86_64.

```bash
git clone https://github.com/Alexander-D-Karpov/MemeSearch && cd MemeSearch
cp .env.example .env
nvim .env
```

In `.env` set at least:

- `POSTGRES_PASSWORD`, and the same password inside `DATABASE_URL`
- `INTERNAL_TOKEN` (`openssl rand -hex 32`)
- `MEDIA_HOST_DIR`, the folder on the external drive
- `FALLBACK_API_KEY` if you want the API fallback
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS` for the bot

```bash
sudo mkdir -p /mnt/memes && sudo chown -R 10001:10001 /mnt/memes

docker compose build
docker compose run --rm --no-deps web hash-password 'your-password'
# put the output into .env as ADMIN_PASSWORD_HASH='<hash>' (keep the single quotes)
docker compose up -d
docker compose ps
docker compose logs -f ml worker          # first start downloads models (~1.5 GB) into the ms_data volume
```

The web app listens on `127.0.0.1:8080`. Put nginx in front of it:

```bash
sudo mkdir -p /var/www/letsencrypt
sudo certbot certonly --nginx -d ms.akarpov.ru
sudo cp deploy/nginx/ms.akarpov.ru.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/ms.akarpov.ru.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

If `MEDIA_HOST_DIR` is not `/mnt/memes`, change the two `alias` lines in the nginx config.

Update later with `git pull && docker compose build && docker compose up -d`. Database migrations run automatically when `web` starts.

The worker and ml containers run under `worker/seccomp-userns.json` (Docker default profile plus the namespace syscalls) because Codex sandboxes itself with bwrap; under the stock profile the sandbox fails with "No permissions to create a new namespace". Regenerate it with `worker/gen-seccomp.py` after a Docker upgrade.

## First steps

1. Open `/login`, then `/admin/codex` → **Add session** → **Login (device code)**, open the link and enter the code. Or import `~/.codex/auth.json`. Add several accounts to rotate; a limited session cools down until its limit resets.
2. `/admin/upload`: drop files or whole folders (parallel batched uploads, duplicates skipped by sha256), upload a `.zip`, or copy files into `MEDIA_HOST_DIR/inbox` and press **Import inbox**.
3. Telegram: set `TELEGRAM_BOT_TOKEN`, send `/start` to get your id, put it into `TELEGRAM_ADMIN_IDS`, restart `bot`. Forward memes; the reply is updated with the title when analysis finishes. `/search`, `/stats`, `/reprocess <id>`.

## Telegram channels

`/admin/channels`: add a public channel (`@name` or `https://t.me/name`). The worker reads the channel's web preview (`t.me/s/name`), no bot or account is needed; private channels and channels with the preview turned off cannot be read.

- First run imports up to **backfill** old posts (0 = whole history), then new posts are fetched every `CHANNEL_POLL_MINUTES`. **Check now** fetches immediately.
- Each imported meme links to its post, and the post text is passed to the analysis as the caption.
- Dedup: exact file (sha256) or same picture (256-bit perceptual hash within `CHANNEL_DEDUP_DISTANCE` bits, compared against the whole library) is not added again; the post link is attached to the existing meme instead, so a meme can show several sources.
- Videos longer than `CHANNEL_MAX_VIDEO_SECONDS` and posts Telegram marks as "media too big" are skipped. Files over `CHANNEL_MAX_FILE_MB` too.
- If t.me is blocked from the server, set `TELEGRAM_WEB_PROXY` (or `TELEGRAM_PROXY`).

## Link previews

Meme pages carry Open Graph and Twitter tags. Images up to 5 MB in jpg/png/gif are used as is; everything else (webp, heic, videos) gets a JPEG preview from `/m/{id}/og.jpg`. Videos also get `og:video`, so Telegram and Discord can play them inline.

## Reprocessing

- Per meme: **Reprocess** (Codex again) or **Re-embed** (vectors only) on the meme page.
- Bulk: select in `/admin/memes`, or dashboard buttons (retry failed, requeue stuck, re-embed all, re-analyze everything).
- Manual edits lock a meme: reprocessing keeps the edited texts and only recomputes vectors. Untick **Locked** to let Codex overwrite it again.

## Search tuning

- `SEARCH_TEXT_MAX_DIST` / `SEARCH_CLIP_MAX_DIST` (cosine distance, default `2` = off) drop vector-only matches that are too far, so nonsense queries return nothing. Start with `0.25` / `0.95` and adjust by looking at results.
- Results are cached in Redis per query and invalidated on every change.

## API

Public: `GET /api/v1/search?q=&kind=image|gif|video&limit=&offset=`, `GET /api/v1/memes?before=`, `GET /api/v1/memes/{id}`, `GET /api/v1/memes/{id}/similar`.

Admin (cookie or `Authorization: Bearer $ADMIN_API_TOKEN`):

```bash
curl -H "Authorization: Bearer $T" -F files=@a.jpg -F files=@b.mp4 https://ms.akarpov.ru/api/v1/admin/upload
curl -H "Authorization: Bearer $T" -F file=@memes.zip https://ms.akarpov.ru/api/v1/admin/import/zip
curl -H "Authorization: Bearer $T" -X POST https://ms.akarpov.ru/api/v1/admin/import/inbox
curl -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"scope":"failed"}' https://ms.akarpov.ru/api/v1/admin/reprocess
curl -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"ids":[1,2],"action":"reprocess"}' https://ms.akarpov.ru/api/v1/admin/memes/bulk
curl -H "Authorization: Bearer $T" -X PATCH -H 'Content-Type: application/json' -d '{"title":"..."}' https://ms.akarpov.ru/api/v1/admin/memes/1
```

## Local development

In `.env` set `SERVE_MEDIA=true`, `COOKIE_SECURE=false`, `PUBLIC_URL=http://localhost:8080`, `MEDIA_HOST_DIR=./data/media`, then:

```bash
mkdir -p data/media && sudo chown -R 10001:10001 data/media
docker compose up --build
```
