# Specification: Xiaomi Cameras NAS RTSP Bridge

**Platform:** ZimaOS NAS with Docker Compose. Blue Iris is a separate LAN host.

## Goal

Expose selected immediate child folders of the ZimaOS host directory
`/DATA/Cameras/xiaomi_camera_videos` as authenticated virtual RTSP
streams at `rtsp://<NAS-LAN-IP>:<RTSP_PORT>/xiaomi/<camera-id>`. Blue Iris records
each stream continuously into its normal BVR history.

## Architecture

```text
ZimaOS host source (read-only) → /recordings in bridge container
     → independent scanner / SQLite queue / FFmpeg worker per enabled camera
     → MediaMTX RTSP/TCP (authenticated) → Blue Iris
```

The bridge UI is published on configurable host `SETUP_UI_PORT` (default 17883)
while retaining container port 8080. It is authenticated and can select only the
mounted recordings root or a descendant. It scans only direct child folders,
preserves saved cameras through temporary scan failures, and permits safe unique
ID edits.
Host bind mounts remain deployment settings (`.env` / ZimaOS app setting), not
an ability granted to the web container.

## Replay policies

- **Near live (default):** first stable scan selects the newest clip by the
  configured ordering, persists it as high-water, marks older records skipped,
  and only queues clips at or after the high-water thereafter. Late older
  archive uploads are skipped. The policy/high-water are durable over restart.
- **Backfill archive:** queues existing clips oldest-first. At real-time replay,
  a source archive that continues growing can remain perpetually behind.
- An initialized camera policy cannot be silently changed. A confirmed,
  CSRF-protected reset clears pending bridge work (not NAS files), selects Near
  live, and establishes a new high-water on the next stable scan.

Per-camera status includes worker state, queue size, playing file, newest
discovered file, high-water, and approximate filesystem-mtime source lag.

## Constraints and security

- Source mount is read-only; source recordings are never altered/deleted.
- SQLite state, logs, health, and setup settings persist in mounted volumes.
- Workers are independently reconciled; one failure does not block another.
- MediaMTX API is private; configurable host `RTSP_PORT` (default 8554) is
  LAN-published and authenticated. Nonstandard port choices are not security
  controls.
- Reader passwords are redacted from API/logs and persist only as MediaMTX
  SHA-256 auth material; setup password uses salted scrypt.
- MediaMTX runtime auth configuration is applied with its supported control API
  and re-applied after server restart.

## Known limitation

Blue Iris history timestamps are replay/re-ingest times, not Xiaomi capture
times. Filesystem mtime is an approximate source-delay signal, not guaranteed
capture time.

## Acceptance checks

1. Multiple enabled folders produce independent RTSP paths and workers.
2. Near live ignores old late uploads and survives restart without losing its
   high-water; a newer clip queues normally.
3. Backfill is oldest-first; reset intentionally returns a camera to Near live.
4. UI enforces login, CSRF, safe path boundaries, unique IDs, and secret
   redaction; configured cameras survive rescan unavailability.
5. Compose validates, source mount is read-only, and completed files do not
   replay after restart (except a clip interrupted mid-playback).
