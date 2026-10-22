# immich-frame-sync

A headless worker that mirrors one Immich album onto an Android picture frame running
Slideshow (`sk.mimac.slideshow`). Every new photo goes through these steps:

1. It's downloaded as the original file (JPG, PNG, WEBP or HEIC; videos are skipped).
2. It's turned upright using its EXIF orientation.
3. It's blur-filled to `FRAME_WIDTH`×`FRAME_HEIGHT`. The whole photo stays visible, and any empty space is filled with a blurred, darkened copy of it.
4. It's saved as a JPEG and uploaded to the frame with a WebDAV `PUT`.

## Setup on Unraid

```sh
git clone https://github.com/jerbanpls/immich-frame-sync.git
cd immich-frame-sync
mkdir -p /mnt/user/appdata/immich-frame-sync
chown 99:100 /mnt/user/appdata/immich-frame-sync
cp .env.example .env        # fill in the Immich and frame settings
docker compose up -d --build
docker logs -f immich-frame-sync
```

On the frame, open Slideshow → Settings → Web server. Enable it and set a username and password.

## Behaviour

- **Only new photos are processed.** `/cache/state.json` records every asset ID already handled.
- **Frame offline:** the processed JPEG stays in `/cache/processed/`, and the upload is retried each cycle without downloading the photo again.
- **Immich unreachable or bad API key:** the cycle is skipped. Nothing is ever deleted because a fetch failed.
- **Photo removed from the album:**
  - With `DELETE_FROM_FRAME=true`, it's deleted from the frame.
  - With `false` (the default), it stays on the frame and the worker forgets about it.
  - Only files named `FRAME_FILE_PREFIX…` (default `immich_`) are ever touched.
- **Corrupt or unsupported file:** it's logged and skipped, and isn't retried.
- **Re-processing everything** (for example after changing the resolution): stop the container, delete `state.json` and `processed/` from the cache folder, then start it again. Existing frame files are overwritten in place because their names stay the same.

Run a single cycle and exit with `docker compose run --rm immich-frame-sync python immich_sync.py --once`.

To update later, run `git pull` and then `docker compose up -d --build`. Your `.env` and cache folder are kept.
