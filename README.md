# Xiaomi Cameras NAS RTSP Bridge

Run this Docker Compose app on a ZimaOS NAS to replay Xiaomi camera backup
folders as independent, authenticated RTSP streams. Source recordings are
mounted read-only and are never changed, moved, or deleted.

```text
/DATA/Cameras/xiaomi_camera_videos (ZimaOS host, read only)
                 │
                 └── /recordings (bridge container)
                         ├── durable queue + FFmpeg worker per camera
                         └── MediaMTX → rtsp://NAS-IP:8554/xiaomi/<camera-id>
                                                   │
                                               RTSP clients
```

## Playback behavior

The default **Near live** policy starts each camera at the newest settled clip,
records a durable high-water mark, and ignores older archive files that arrive
late. This avoids replaying an initial seven-day backup indefinitely. Normal
delay is approximately one source clip plus the configured 90-second settle
period.

Choose **Backfill archive** only when you intentionally want oldest-first
playback. A growing archive replayed at 1× speed may never catch up. Downstream
clients timestamp replayed video when they receive it; the bridge cannot
reconstruct the original capture timeline. The source-lag value in the UI is
an approximation based on NAS filesystem modification time.

The **Start from newest** action skips pending bridge queue entries and returns
one camera to Near live. It never removes source files.

## Install from the ZimaOS market

The published market is the quickest installation route and requires no SSH:

```text
https://dparvanov.github.io/xiaomi-cameras-nas-rtsp/store.json
```

1. Confirm that `/DATA/Cameras/xiaomi_camera_videos` exists on the NAS and
   contains one immediate child folder per camera.
2. In ZimaOS, add the URL above as a custom market and install **Xiaomi Cameras
   NAS RTSP Bridge**.
3. Open the app at `http://<NAS-IP>:17883`.
4. On first launch, create the administrator account in the browser. There is
   no default username or password.
5. Set RTSP client credentials, enable the desired folders, and save.
6. Add `rtsp://<username>:<password>@<NAS-IP>:8554/xiaomi/<camera-id>` to an
   RTSP-compatible viewer or recorder, using RTSP over TCP.

The administrator password is stored as a salted scrypt hash. The app generates
its own persistent session key under `/DATA/AppData/xiaomi-cameras-nas-rtsp/data`.
RTSP passwords are never returned by the UI/API and persist only as the hash
format used by MediaMTX.

State-changing forms require a random per-session CSRF token, use a
`SameSite=Lax` session cookie, and reject browser requests explicitly marked as
cross-site. This remains compatible with ZimaOS routing that can rewrite the
form `Origin` header.

The direct generated Compose URL is:

```text
https://dparvanov.github.io/xiaomi-cameras-nas-rtsp/apps/io.github.xiaomi-cameras-nas-rtsp/docker-compose.yml
```

Every release also has a cache-resistant direct installer at:

```text
https://dparvanov.github.io/xiaomi-cameras-nas-rtsp/releases/<version>/docker-compose.yml
```

Use that version-specific URL with ZimaOS **Install Custom App → External
link** if a market update starts an older cached container. Published Compose
files request a fresh image pull, and the running build is shown in the top
bar and in the `X-Xiaomi-Cameras-RTSP-Version` HTTP response header.

## Configure cameras

The page scans immediate child directories of `/recordings`. The optional
subdirectory setting can narrow that scan to a safe descendant; absolute paths
and `..` are rejected. Changing the host bind mount itself requires a Compose
change because the web container cannot access arbitrary NAS paths.

Each enabled folder has:

- a display name;
- a unique stream ID, producing `/xiaomi/<camera-id>`;
- a Near live or Backfill initial policy;
- independent queue, playback, newest-file, and source-lag status.

Unavailable folders retain their saved configuration. Applying settings only
reconciles workers that changed and does not rebuild the image.

## RTSP client settings

Use one client input per enabled camera:

| Setting | Value |
|---|---|
| Host | NAS LAN address, such as `192.168.68.93` |
| Port | `8554` by default |
| Username/password | Credentials created on the setup page |
| Path | `/xiaomi/<camera-id>` |
| Transport | TCP |

An idle stream may report no signal until a stable source clip is available;
the bridge does not manufacture placeholder video.

## Developer deployment

For a source build on the NAS:

```sh
mkdir -p config data logs
cp .env.example .env
cp config.example.json config/config.json
cp mediamtx.yml config/mediamtx.yml
./scripts/preflight.sh
docker compose up -d --build
```

Set `XIAOMI_RECORDINGS_PATH` in `.env` if the host folder differs from
`/DATA/Cameras/xiaomi_camera_videos`. Replace the publisher-password placeholder
with a unique secret. The administrator is still created in the browser; no
admin or session-secret environment values are needed.

The local Custom App import route requires building
`xiaomi-cameras-nas-rtsp:local` first and importing
`zimaos-import.compose.yml`. Set **Primary Service** to `bridge`, Web URL port
to `17883`, path to `/`, and keep the RTSP port mapping at `8554` unless a
different free host port is required.

## Release and market publication

Pushing a `v*` tag publishes immutable `linux/amd64` and `linux/arm64` bridge
and MediaMTX images to GHCR. The workflow then uses the official AppStore v2
builder to generate the market and deploy it to GitHub Pages. Source metadata
lives under `market/Apps/XiaomiCamerasNasRtsp`; generated `market/dist` files
should not be edited by hand.

## Security

- Only RTSP/TCP `8554` and the setup page/TCP `17883` are published by default;
  the MediaMTX control API remains inside the Docker network.
- The recordings bind mount is read-only.
- Keep both ports on a trusted LAN, do not port-forward them, and use a VPN for
  remote access. RTSP authentication does not encrypt the video transport.
- A nonstandard port avoids collisions but is not a security boundary.
- If the UI is placed behind HTTPS, set `SETUP_COOKIE_SECURE=true`.
- Reader access is limited to `xiaomi/...`; the internal bridge identity owns
  publishing and control permissions.

## Troubleshooting

- **The old interface appears after an update:** uninstall the app without
  deleting AppData, then install the version-specific external Compose URL.
  The dashboard version badge and HTTP response header must match the release.
- **No folders:** confirm the host path exists, then run
  `docker compose exec bridge ls -la /recordings` for a developer deployment.
- **An RTSP client cannot connect:** check the host port/firewall, client
  credentials, `/xiaomi/<camera-id>` path, and MediaMTX logs.
- **A clip retries or is skipped:** inspect
  `logs/<camera-id>.ffmpeg.log`. Failures are isolated per camera.
- **High source lag:** Near live skips old archive backlog; Backfill does not.
  Wait for Xiaomi's initial backup to finish before treating mtime-based lag as
  a steady-state measurement.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
docker compose config -q
```
