# Xiaomi Cameras NAS RTSP Bridge

Run this Docker Compose project on the ZimaOS NAS. It reads Xiaomi recordings
from a **read-only** local NAS folder, replays each selected camera folder as
its own RTSP stream, and lets Blue Iris record those streams as normal cameras.

```text
/DATA/Cameras/xiaomi_camera_videos (ZimaOS host, read only)
                 │
                 └── /recordings (bridge container)
                         ├── one worker + durable SQLite queue per camera
                         └── MediaMTX → rtsp://NAS-LAN-IP:RTSP_PORT/xiaomi/<camera-id>
                                                        │
                                                     Blue Iris
```

The bridge never changes, moves, or deletes Xiaomi source recordings.

## Important: timestamps and delay

Blue Iris assigns history timestamps when it receives the replay, **not** at
the original Xiaomi capture time. This cannot be corrected by the bridge
without falsifying the Blue Iris clock. The UI's source-lag number is only an
approximation based on NAS filesystem modification time; Xiaomi's backup time
may differ from its capture time.

The recommended default is **Near live**. When a camera is enabled for the
first time, the bridge finds the newest *settled* clip by the configured
ordering, stores it as a durable high-water mark, marks older existing clips
as skipped, and then accepts only clips newer than that mark. This prevents a
continuously uploading seven-day Xiaomi archive from leaving Blue Iris seven
days behind forever. Late uploads whose ordering key is older than the mark are
ignored. The newest settled clip may play once, so ordinary ongoing delay is
roughly clip duration plus the 90-second settle period.

Choose **Backfill archive** only when you intentionally want oldest-first
replay. At 1× replay, a continuously growing archive can never catch up, and
Blue Iris will still timestamp it at replay time. You can wait until Xiaomi's
initial backup is complete to measure steady-state delay, but configuring the
bridge now in Near live mode is safe: it will not chase late-arriving old
archive paths.

## ZimaOS deployment

Your SMB share is:

```text
smb://192.168.68.93/Cameras/xiaomi_camera_videos
```

The service runs on that same NAS. Use the host filesystem path behind the
share—not the `smb://` URL. The supplied deployment default is:

```text
/DATA/Cameras/xiaomi_camera_videos
```

That directory is bind-mounted read-only as `/recordings`. Confirm it contains
one direct child folder per camera before starting. If your ZimaOS installation
uses another path, change `XIAOMI_RECORDINGS_PATH` in `.env`.

### No-SSH registry installation (after publication)

This is the intended end-user path once a maintainer has published the images
and market. It requires no source transfer, terminal, local image build, or
host configuration files. Docker/ZimaOS creates the persistent
`/DATA/AppData/xiaomi-cameras-nas-rtsp/data` and `logs` directories; the only
pre-existing host path required is the read-only Xiaomi recording directory.

1. In ZimaOS, add the published custom-market URL, or use **Install Custom App
   → External Link** with the published generated Compose URL. Do not use the
   source template before it has been built for a release.
2. In the installation form, replace every credential placeholder. The
   `RTSP_PUBLISH_PASSWORD` and MediaMTX publisher password must be the same,
   setup credentials must be unique, and the session secret must be at least 32
   characters.
3. Confirm **Primary Service** is `bridge`, the Web URL is `http` on port
   `17883` at `/`, and RTSP is TCP `8554`; then install.
4. Open `http://192.168.68.93:17883`, sign in, select camera folders, and set
   the Blue Iris reader credentials.

After a successful GitHub Pages market deployment, the URL patterns are:

```text
Custom market: https://<GITHUB_OWNER>.github.io/<REPOSITORY>/store.json
Direct Compose: https://<GITHUB_OWNER>.github.io/<REPOSITORY>/apps/io.github.xiaomi-cameras-nas-rtsp/docker-compose.yml
```

Neither URL nor a registry image exists until the maintainer completes the
publication steps below. The checked-in [registry template](zimaos-one-click.compose.yml)
is deliberately not directly installable: it contains image-owner, release-tag,
and credential placeholders to be resolved before import.

### Release and publication (maintainer)

Choose one GitHub owner/organization and create or push this project as its
`xiaomi-cameras-nas-rtsp` repository. That owner is the single image-namespace
choice: the release workflow derives and publishes these images automatically:

```text
ghcr.io/<GITHUB_OWNER>/xiaomi-cameras-nas-rtsp:<RELEASE_VERSION>
ghcr.io/<GITHUB_OWNER>/xiaomi-cameras-nas-rtsp-mediamtx:<RELEASE_VERSION>
```

Before the first release, allow GitHub Actions to write packages and enable
GitHub Pages with **Source: GitHub Actions**. After the first image publish,
set both GHCR packages to public if ZimaOS should pull them without registry
credentials. Create and push a release tag such as `v1.0.0`.

The release workflow builds both `linux/amd64` and `linux/arm64` images with
immutable `1.0.0`-style tags, then uses the official AppStore v2 build action
to turn [market](market) into `market/dist/` and deploy it to GitHub Pages.
It also emits moving major/minor and `latest` convenience tags, but the market
and direct Compose point at the immutable release version. Do not edit generated
`dist/` files by hand.

The market source owns its icon and AppStore metadata under
[market/Apps/XiaomiCamerasNasRtsp](market/Apps/XiaomiCamerasNasRtsp); it is distinct
from the local-image and developer routes below.

### Developer/SSH deployment

1. Transfer/copy this complete project directory to the persistent ZimaOS
   location `/DATA/AppData/xiaomi-cameras-nas-rtsp`. The normal Compose
   file contains `build: .`, so it must run from a directory containing the
   source and `Dockerfile`; pasting it into Custom App alone cannot build the
   bridge image.

2. In the ZimaOS terminal or SSH session, change into that directory and
   create the persistent folders/configuration:

   ```sh
   mkdir -p config data logs
   cp .env.example .env
   cp config.example.json config/config.json
   cp mediamtx.yml config/mediamtx.yml
   ```

3. Edit `.env` and replace every `REPLACE_WITH...` placeholder. Generate each
   secret in a private terminal, for example with `openssl rand -base64 36`; do
   not commit or expose generated values in shared logs/chat. The setup username cannot
   be `admin` or `administrator`; the setup password needs at least 16
   characters and the session secret at least 32. Keep `SETUP_UI_PORT=17883`
   for the less-common web port, or choose a free TCP port. `RTSP_PORT=8554` is
   independently configurable.

4. Verify the deployment without printing secrets, then build and start it:

   ```sh
   ./scripts/preflight.sh
   docker compose up -d --build
   docker compose ps
   ```

5. From a trusted LAN device, open `http://192.168.68.93:17883` (replace with
   the NAS LAN IP) and sign in with the initial setup credentials from `.env`.
   On the page, select `/recordings` or a safe child directory, scan the direct
   child folders, enable the cameras you want, and assign unique camera IDs.
   Each ID becomes `/xiaomi/<camera-id>`. Configure the Blue Iris RTSP reader
   username/password there as well. Passwords are never returned by the UI/API
   or stored in plaintext in the persistent settings file.

The web page cannot change arbitrary host paths or Docker bind mounts. This is
a deliberate boundary: change `XIAOMI_RECORDINGS_PATH` in `.env` (or the ZimaOS
app's equivalent environment setting) and recreate the bridge service if the
host mapping itself needs to change. The page can select only `/recordings` or
a subdirectory inside it; `..` and absolute paths are rejected.

Configured camera entries are preserved if a folder is temporarily unavailable
during a rescan. Saving settings reconciles only added, removed, or changed
workers; it does not rebuild images or disturb unchanged streams.

### ZimaOS Custom App import (local image)

Use this path when you are already at **Install Custom App → Import → Docker
Compose**. It is intentionally a two-stage local deployment, not an App Center
one-click app: the local image must exist before ZimaOS can show `bridge` as a
primary service.

1. Complete steps 1–3 above, then build the local image from the project source
   directory:

   ```sh
   cd /DATA/AppData/xiaomi-cameras-nas-rtsp
   docker build --tag xiaomi-cameras-nas-rtsp:local .
   ```

2. Keep `config/config.json`, `config/mediamtx.yml`, `data`, and `logs` under
   `/DATA/AppData/xiaomi-cameras-nas-rtsp`. The import definition uses
   those absolute paths and mounts the Xiaomi source read-only from
   `/DATA/Cameras/xiaomi_camera_videos`.

3. Import or paste [zimaos-import.compose.yml](zimaos-import.compose.yml) in
   ZimaOS. In its YAML or the Form, replace every `REPLACE_WITH...` environment
   value with the same secrets used in `.env`; never leave placeholders.

4. Set **Primary Service** to **bridge**—not `mediamtx`. Set the Web URL scheme
   to `http`, port to `17883`, and path to `/`, then install. Open
   `http://192.168.68.93:17883` afterward.

To choose different host ports in the import form, change the left-hand side of
the port mappings (`17883:8080` for the UI and `8554:8554` for RTSP). Keep
`x-casaos.port_map` aligned with the UI host port and use the chosen RTSP port
in Blue Iris. A port change is not a security boundary.

The import file deliberately contains no `.env` interpolation, because Custom
App import forms need not resolve a separate `.env`. It requires the locally
built `xiaomi-cameras-nas-rtsp:local` image; it does not download a bridge image.

To update, copy new source into the same project directory, rebuild the same
tag with `docker build --tag xiaomi-cameras-nas-rtsp:local .`, then recreate or
restart the app from ZimaOS. Re-import only if the Compose definition changes;
persistent configuration, state, and logs remain in AppData.

### Start from newest now

Each camera has a deliberate **Start from newest now** action. It requires a
confirmation checkbox and CSRF-protected form submission. It removes pending
bridge queue entries only—never NAS files—switches that camera to Near live,
and makes the next stable scan establish a new high-water mark. Use it to
abandon a backfill or to intentionally skip a growing backlog.

## Blue Iris

After the camera is enabled and its worker shows `waiting` or `streaming`, add
one **Network IP** camera in Blue Iris for each path. Use the NAS LAN address,
not `localhost` or `127.0.0.1`.

| Blue Iris field | Value |
|---|---|
| Address | `192.168.68.93` (or your NAS LAN IP) |
| RTSP port | value of `RTSP_PORT` (default `8554`) |
| Username / password | the reader credentials set in the web page |
| Main stream path | `/xiaomi/<camera-id>` |
| Transport | TCP |
| Record mode | Continuous |
| File format | Blue Iris DVR (`.bvr`) |

For the defaults: `rtsp://blueiris:YOUR_PASSWORD@192.168.68.93:8554/xiaomi/front-door`.
Blue Iris can show no signal when no stable source clip is ready; the bridge
does not generate fake black video merely to keep an idle tile online.

## Security

- Only RTSP/TCP `RTSP_PORT` (default 8554) and the authenticated setup page/TCP
  `SETUP_UI_PORT` (default 17883) are published. The MediaMTX API is private to
  the Docker network.
- Reader credentials have read-only permission on `xiaomi/...`; bridge
  credentials have publish/API permissions only.
- Do not port-forward either port. Use a VPN for remote access. Where possible,
  firewall the selected RTSP port to the Blue Iris computer and the selected UI
  port to trusted LAN administration devices.
- A nonstandard port reduces accidental collisions; it is **not** a security
  control. Keep both services off the public Internet.
- RTSP is authentication, not encryption. Keep it on the trusted home LAN.
  If you place the setup page behind HTTPS, set `SETUP_COOKIE_SECURE=true`.
- The RTSP access rule is hot-applied through MediaMTX's supported control API
  and is re-applied after a MediaMTX restart. No image rebuild is needed.

## SMB/CIFS fallback

Only if ZimaOS cannot expose the local backing directory to Docker, mount the
share on the **host** using the ZimaOS-supported mechanism, then bind mount
that local mount as `XIAOMI_RECORDINGS_PATH`. Do not put an `smb://` URL in the
compose volume value and do not mount SMB inside the bridge container. For a
generic Linux host:

```sh
sudo mkdir -p /mnt/cameras
sudo mount -t cifs //192.168.68.93/Cameras /mnt/cameras -o ro,vers=3.0,username=YOUR_SMB_USER
```

Use `/mnt/cameras/xiaomi_camera_videos` as `XIAOMI_RECORDINGS_PATH`; keep SMB
credentials out of Compose and make the host mount persistent before relying
on it.

## Troubleshooting

- **NAS unavailable:** verify the host path and run
  `docker compose exec bridge ls -la /recordings`.
- **Blue Iris cannot connect:** check MediaMTX logs, the configured
  `RTSP_PORT`/firewall rules, reader credentials, and that the configured path
  is `/xiaomi/<camera-id>`.
- **A worker retries/skips a clip:** inspect `logs/<camera-id>.ffmpeg.log`.
  Failures are retried three times and are isolated to that camera.
- **High source lag:** check the selected policy. Near live skips old archive
  backlog; Backfill intentionally does not. Wait for Xiaomi's initial upload
  to finish before treating filesystem-mtime lag as a steady-state measure.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
docker compose config -q
```
