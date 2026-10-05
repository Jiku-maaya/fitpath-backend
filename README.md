# FitPath shared AI backend

Prepared for deployment; no server has been provisioned or published. No Groq credential is embedded in the APK or these files. Personal Groq keys work directly with Groq as before and take priority over shared access.

## Deploy

Build this directory with Docker, or run `python server.py` using Python 3.13+. Configure `GROQ_API_KEY` and a random `IP_HASH_SALT` of at least 32 characters through your host's secret settings. Set `DATA_DIR` to a persistent writable volume. The container uses UID 10001. Serve through an HTTPS reverse proxy and configure its body limit to 9 MB, upstream timeout to 60 seconds, concurrent connection limits and request throttling. Do not expose port 8080 directly to the internet. No credentials are needed for `/health`.

Run one service replica with a persistent SQLite volume. Atomic reservations enforce 3 requests/minute and 20/day per socket-peer IP, 200 requests/day globally, and 40 photo requests/day globally. Quotas survive restarts, are UTC fixed windows, and rejected/failed provider attempts still consume a reserved request. At most one 6 MB image, 60,000 text characters and 8,192 output tokens per request. No tools, remote image URLs, model overrides or arbitrary upstream URLs are accepted. Set a separate project spending limit in Groq Console.

The service ignores forwarded-IP headers by default. Behind a reverse proxy all callers initially share its IP quota. To enable per-client IP quotas, set `TRUSTED_PROXY_IPS` to the exact socket-peer addresses of your reverse proxy (comma-separated) and configure the proxy to **overwrite** `X-Real-IP` with the actual client IP. For example Nginx uses `proxy_set_header X-Real-IP $remote_addr;`. Restrict direct service access to that proxy. Requests from other peers cannot spoof client identity; requests from a trusted proxy with a missing/invalid client IP fail closed. For public scale, add verified account authentication plus distributed quotas before adding replicas; anonymous IP limits are not individual-user limits. Global caps bound usage even when a caller changes IP addresses. This gateway is a small deployment starting point, not a multi-tenant authentication system.

Text defaults to GPT-OSS 20B; photos use Qwen 3.8 27B (preview). `TEXT_MODEL` and `PHOTO_MODEL` can override these on the server. Model availability still depends on Groq project access.

After deploying, enter `https://YOUR_HOST/v1/analyze` in FitPath Settings → Groq & voice → Shared AI service. Alternatively build a future APK with `--dart-define=FITPATH_AI_URL=https://YOUR_HOST/v1/analyze`. Remove the personal key to use shared access. Never put the shared Groq key in Dart defines or app assets.

## Verify

Run `python -m unittest discover -s backend -p "test_*.py"` from the project root. Tests use synthetic credentials and no live Groq requests. Then test `/health`, an authorized synthetic text request, quota exhaustion (HTTP 429 + Retry-After), and a food photo against the deployed host. Actual live access is not verified until deployment.
