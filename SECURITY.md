# Security Policy

## Where your data goes

squawk is designed to keep your audio and credentials under your control:

- **Audio stays on-device except for the transcription request.** Recordings are
  written only under `$TMPDIR` and deleted after transcription (unless you pass
  `--keep-audio`). The only time audio leaves your machine is the request to the
  **speech endpoint you configured** via `SQUAWK_SPEECH_URL` — squawk sends it
  nowhere else, and there is no telemetry, analytics, or third-party call.
- **You choose the endpoint.** squawk ships with no backend baked in. It can talk
  to a local server, a service on your LAN, or a hosted provider — whatever you
  point `SQUAWK_SPEECH_URL` at. Your privacy posture is whatever that endpoint's
  is.
- **Keys stay local.** The API key is read from `~/.config/squawk/config`
  (create it `chmod 600`) or the `SQUAWK_SPEECH_KEY` environment variable. It is
  never hardcoded, logged, or committed. Keep the config file out of version
  control.

## Reporting a vulnerability

If you find a security issue, please report it privately to the maintainers
rather than opening a public issue, and allow reasonable time for a fix before
disclosure.
