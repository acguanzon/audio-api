# Self-hosted audio conversion API

This service accepts an authorised public YouTube URL, creates an MP3 with
`yt-dlp` and FFmpeg, and returns a one-time, expiring download token.

> Only use this service for media you own or are authorised to download. You are
> responsible for complying with YouTube's terms, copyright law, and the laws
> where you operate the server.

## Run with Docker

1. Install Docker Desktop or Docker Engine on the server.
2. Copy `.env.example` to `.env` and set a strong random `AUDIO_API_KEY`.
3. Start the service:

```sh
docker compose up --build -d
```

4. Check the service:

```sh
curl http://localhost:8080/health
```

For a phone outside your home network, deploy this behind an HTTPS reverse
proxy such as Caddy or Nginx. Do not expose it publicly without HTTPS,
authentication, rate limiting, and monitoring.

## API

All endpoints except `/health` require this header:

```text
Authorization: Bearer <AUDIO_API_KEY>
```

Create a conversion:

```sh
curl -X POST https://your-domain.example/v1/conversions \
  -H 'Authorization: Bearer YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

A successful response contains a `token`. Download the MP3 with:

```sh
curl -L https://your-domain.example/v1/downloads/TOKEN \
  -H 'Authorization: Bearer YOUR_KEY' \
  -o audio.mp3
```

Tokens are one-time use and expire after five minutes by default. The generated
file is deleted after its download response finishes.

## Production notes

- Set `MAX_CONCURRENT_JOBS=1` unless your server has sufficient CPU/RAM.
- Keep `RATE_LIMIT_PER_HOUR` low to control bandwidth and storage costs.
- Store `.env` only on the server; never put `AUDIO_API_KEY` in a public APK.
  A production mobile app should authenticate users with your backend, which
  then keeps this converter key private.
- Update `yt-dlp` regularly because upstream extractor changes are common.

## YouTube bot-check cookies

Cloud-hosting IP addresses can receive YouTube's **"Sign in to confirm you're
not a bot"** response. When that occurs, the server needs an authenticated,
Netscape-format YouTube cookie file. Do not commit, upload, or share this file.
Use a dedicated account and follow YouTube's terms and content permissions.

For Render, encode the exported `youtube-cookies.txt` locally, then add the
single-line output as the Render environment secret `YTDLP_COOKIES_B64`:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("youtube-cookies.txt"))
```

Redeploy the service after saving the secret. The service writes the cookie only
to its temporary filesystem with owner-only permissions and configures `yt-dlp`
to use it. Refresh the cookie if YouTube invalidates the session.
