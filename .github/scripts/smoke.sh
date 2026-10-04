#!/usr/bin/env bash
set -euo pipefail

BASE=http://127.0.0.1:8080
AUTH="Authorization: Bearer ${ADMIN_API_TOKEN}"
WORK=${RUNNER_TEMP:-/tmp}/smoke
mkdir -p "$WORK" "$UPLOAD_DIR"

fail() { echo "SMOKE FAILED: $*" >&2; cat "$WORK/web.log" >&2 || true; exit 1; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

./memesearch >"$WORK/web.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$BASE/healthz" | grep -q '"db":"ok"' || fail "healthz"

python3 - "$WORK/meme.png" <<'PY'
import base64, sys
open(sys.argv[1], "wb").write(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="))
PY

out=$(curl -fsS -H "$AUTH" -F "files=@$WORK/meme.png" "$BASE/api/v1/admin/upload")
[ "$(echo "$out" | json 'd["added"]')" = "1" ] || fail "upload: $out"
ID=$(echo "$out" | json 'd["results"][0]["id"]')

out=$(curl -fsS -H "$AUTH" -F "files=@$WORK/meme.png" "$BASE/api/v1/admin/upload")
[ "$(echo "$out" | json 'd["results"][0]["duplicate"]')" = "True" ] || fail "duplicate: $out"

out=$(curl -fsS -H "$AUTH" -F "files=@$0" "$BASE/api/v1/admin/upload")
echo "$out" | grep -q "unsupported media type" || fail "non-media accepted: $out"

URL=$(curl -fsS -H "$AUTH" "$BASE/api/v1/memes?status=pending" | json 'd["memes"][0]["url"]')
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$URL")" = "200" ] || fail "media not served at $URL"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/media/.tmp/x")" = "404" ] || fail "hidden media dir served"

[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/m/$ID")" = "404" ] || fail "pending meme is public"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin")" = "303" ] || fail "admin without login"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/admin/stats")" = "401" ] || fail "admin api without token"

curl -fsS -H "$AUTH" -H 'Content-Type: application/json' -X PATCH \
  -d '{"title":"Кот узнал слово кубернетес","tags":["кот","kubernetes"]}' "$BASE/api/v1/admin/memes/$ID" >/dev/null
psql "$DATABASE_URL" -qc "UPDATE memes SET status='done' WHERE id=$ID"

for q in "кубернетес" "кубернетис" "kubernetes"; do
  ids=$(curl -fsS -G "$BASE/api/v1/search" --data-urlencode "q=$q" | json '[m["id"] for m in d["memes"]]')
  echo "$ids" | grep -q "\b$ID\b" || fail "search '$q' returned $ids"
done

page=$(curl -fsS "$BASE/m/$ID") || fail "meme page"
echo "$page" | grep -q "<meta property=\"og:image\" content=\"$BASE/media/originals/" || fail "og:image missing"
echo "$page" | grep -q '<meta name="twitter:card" content="summary_large_image">' || fail "twitter card missing"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/m/$ID/og.jpg")" = "404" ] || fail "og.jpg without thumb"
[ "$(curl -s -o /dev/null -w '%{content_type}' "$BASE/m/$ID/photo.jpg")" = "image/jpeg" ] || fail "photo.jpg from png original"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/?q=%D0%BA%D0%BE%D1%82")" = "200" ] || fail "search page"

code=$(curl -s -o "$WORK/ch.json" -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"channel":"https://t.me/s/Some_Memes","backfill_limit":10}' "$BASE/api/v1/admin/channels")
[ "$code" = "201" ] || fail "add channel: $code $(cat "$WORK/ch.json")"
[ "$(json 'd["username"]' < "$WORK/ch.json")" = "some_memes" ] || fail "channel name: $(cat "$WORK/ch.json")"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' -d '{"channel":"@some_memes"}' "$BASE/api/v1/admin/channels")
[ "$code" = "409" ] || fail "duplicate channel: $code"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "$BASE/admin/channels")" = "200" ] || fail "channels page"
psql "$DATABASE_URL" -qc "INSERT INTO meme_sources (meme_id, url, source, post_id) VALUES ($ID, 'https://t.me/some_memes/5', 'telegram', 5)"
curl -fsS "$BASE/m/$ID" | grep -q 'https://t.me/some_memes/5' || fail "source link missing on meme page"

psql "$DATABASE_URL" -qc "UPDATE memes SET tg_cache_error='boom', tg_cache_attempts=3 WHERE id=$ID"
[ "$(curl -fsS -H "$AUTH" -X POST "$BASE/api/v1/admin/tgcache/retry" | json 'd["queued"]')" = "1" ] || fail "telegram upload retry"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "$BASE/admin")" = "200" ] || fail "dashboard"

curl -fsS "$BASE/api/v1/memes/$ID/similar?mode=looks" | grep -q '"memes"' || fail "look-alike api"

out=$(curl -s -H "Accept: application/json" -F "image=@$0" "$BASE/api/v1/search/image")
echo "$out" | grep -q "not a picture" || fail "image search accepted a non-image: $out"
curl -fsS "$BASE/?img=00000000000000000000000000000000" | grep -q "expired" || fail "expired picture search"
curl -fsS "$BASE/m/$ID" | grep -q "?like=$ID" || fail "find similar pictures link"

echo "smoke test passed"
