# Specification: Xiaomi Cameras NAS RTSP Bridge

**Platform:** ZimaOS NAS with Docker Compose and generic RTSP clients on the LAN.

## Goal

Expose selected immediate child folders of
`/DATA/Cameras/xiaomi_camera_videos` as authenticated streams at
`rtsp://<NAS-LAN-IP>:<RTSP_PORT>/xiaomi/<camera-id>`.

## Architecture

```text
ZimaOS recordings (read-only) → /recordings in bridge container
    → scanner / SQLite queue / FFmpeg worker per enabled camera
    → authenticated MediaMTX RTSP/TCP → RTSP viewers and recorders
```

The UI uses configurable host port `17883` and container port `8080`. First
launch creates an administrator account in the browser; no default credentials
exist. A random session key is generated and persisted in AppData. The UI can
select only the recordings mount or a descendant, scans only direct children,
preserves temporarily unavailable cameras, and supports safe unique stream IDs.
Host bind mounts remain deployment settings rather than a capability granted to
the web container.

## Replay policies

- **Near live (default):** the first stable scan selects the newest clip,
  persists it as high-water, marks older records skipped, and queues only clips
  at or after that point. Late older uploads remain skipped across restarts.
- **Backfill archive:** existing clips queue oldest-first. At real-time replay,
  a source archive that continues growing can remain perpetually behind.
- An initialized policy cannot change silently. A confirmed CSRF-protected
  reset clears pending bridge work—not NAS files—and reinitializes Near live.

Per-camera status includes worker state, queue size, playing file, newest file,
high-water, and approximate filesystem-mtime source lag.

## Constraints and security

- Source recordings are mounted read-only and never altered or deleted.
- SQLite state, logs, health, settings, and session material persist in mounted
  AppData volumes.
- Workers reconcile independently; one camera failure does not block another.
- MediaMTX's control API is private. RTSP is LAN-published, authenticated, and
  restricted to `xiaomi/...` for client accounts.
- Client secrets are redacted from API/logs and stored only as MediaMTX SHA-256
  auth material; administrator passwords use salted scrypt.
- The login uses throttling, session rotation, HttpOnly/SameSite cookies, CSRF
  tokens, same-origin validation, and no-store response headers.

## Known limitation

Downstream clients assign timestamps at replay/re-ingest time. The original
capture timeline is not reconstructed, and filesystem mtime is only an
approximate source-delay signal.

## Acceptance checks

1. Multiple enabled folders produce independent RTSP paths and workers.
2. Near live ignores old late uploads and retains high-water across restart.
3. Backfill is oldest-first; reset intentionally returns a camera to Near live.
4. A fresh or legacy-placeholder install opens first-run setup; a real legacy
   administrator remains valid.
5. UI enforces authentication, CSRF, safe paths, unique IDs, and secret
   redaction while preserving cameras through scan failures.
6. Compose validates, the source mount is read-only, and completed clips do not
   replay after restart except for an interrupted in-progress clip.
