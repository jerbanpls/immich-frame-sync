"""Mirror an Immich album onto an sk.mimac.slideshow picture frame.

Polls the album, blur-fills new photos to the frame resolution, PUTs them over WebDAV and
(optionally) deletes photos from the frame when they leave the album.

    python immich_sync.py          # run forever, every SYNC_INTERVAL_MINUTES
    python immich_sync.py --once   # single cycle, then exit
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

import processor
from frame_client import FrameClient, FrameError

log = logging.getLogger("immich-frame-sync")


# --- config ---------------------------------------------------------------

def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    immich_url: str
    immich_api_key: str
    immich_album_id: str
    frame_url: str
    frame_user: str
    frame_pass: str
    frame_webdav_path: str
    frame_width: int
    frame_height: int
    file_prefix: str
    jpeg_quality: int
    interval_minutes: int
    delete_from_frame: bool
    cache_path: Path

    @classmethod
    def from_env(cls) -> "Config":
        missing = [v for v in ("IMMICH_URL", "IMMICH_API_KEY", "IMMICH_ALBUM_ID", "FRAME_URL") if not os.getenv(v)]
        if missing:
            sys.exit(f"Missing required environment variables: {', '.join(missing)}")
        env = os.getenv
        return cls(
            immich_url=env("IMMICH_URL").rstrip("/"),
            immich_api_key=env("IMMICH_API_KEY"),
            immich_album_id=env("IMMICH_ALBUM_ID"),
            frame_url=env("FRAME_URL").rstrip("/"),
            frame_user=env("FRAME_USER", "admin"),
            frame_pass=env("FRAME_PASS", ""),
            frame_webdav_path=env("FRAME_WEBDAV_PATH", "/webdav/"),
            frame_width=int(env("FRAME_WIDTH", "1920")),
            frame_height=int(env("FRAME_HEIGHT", "1200")),
            file_prefix=env("FRAME_FILE_PREFIX", "immich_"),
            jpeg_quality=int(env("JPEG_QUALITY", "88")),
            interval_minutes=max(1, int(env("SYNC_INTERVAL_MINUTES", "15"))),
            delete_from_frame=_bool(env("DELETE_FROM_FRAME", "false")),
            cache_path=Path(env("CACHE_PATH", "/cache")),
        )


# --- Immich ---------------------------------------------------------------

class ImmichError(Exception):
    pass


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, album_id: str):
        self.album_id = album_id
        self._client = httpx.Client(
            base_url=f"{base_url}/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            raise ImmichError(f"Immich {method} {url}: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ImmichError(f"Immich unreachable ({type(exc).__name__})") from exc

    def album_images(self) -> list[dict]:
        """Return [{id, originalFileName}] for every IMAGE asset in the album."""
        album = self._call("GET", f"/albums/{self.album_id}", params={"withoutAssets": "false"}).json()
        assets = album.get("assets") or []
        if not assets and album.get("assetCount"):
            # Newer Immich versions may omit assets from the album response.
            assets = self._search_album_assets()
        return [
            {"id": a["id"], "originalFileName": a.get("originalFileName") or a["id"]}
            for a in assets
            if a.get("type", "IMAGE") == "IMAGE"
        ]

    def _search_album_assets(self) -> list[dict]:
        assets, page = [], 1
        while page:
            body = {"albumIds": [self.album_id], "type": "IMAGE", "page": page, "size": 500}
            result = self._call("POST", "/search/metadata", json=body).json()["assets"]
            assets.extend(result.get("items", []))
            page = int(result["nextPage"]) if result.get("nextPage") else 0
        return assets

    def download_original(self, asset_id: str) -> bytes:
        return self._call("GET", f"/assets/{asset_id}/original", headers={"Accept": "*/*"}).content


# --- state ----------------------------------------------------------------

def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class StateStore:
    """asset_id -> {"file": frame file name or None, "status": uploaded|pending|skipped, "added": iso ts}"""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.assets: dict[str, dict] = json.loads(path.read_text()).get("assets", {})
        except FileNotFoundError:
            self.assets = {}

    def set(self, asset_id: str, file: str | None, status: str) -> None:
        self.assets[asset_id] = {
            "file": file,
            "status": status,
            "added": self.assets.get(asset_id, {}).get("added") or datetime.now(timezone.utc).isoformat(),
        }
        self.save()

    def drop(self, asset_id: str) -> None:
        if self.assets.pop(asset_id, None) is not None:
            self.save()

    def save(self) -> None:
        _atomic_write(self.path, json.dumps({"assets": self.assets}, indent=2).encode())


# --- sync engine ----------------------------------------------------------

class SyncEngine:
    def __init__(self, cfg: Config, immich: ImmichClient, frame: FrameClient, stop: threading.Event):
        self.cfg = cfg
        self.immich = immich
        self.frame = frame
        self.stop = stop
        self.processed_dir = cfg.cache_path / "processed"
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat = cfg.cache_path / "heartbeat"
        self.state = StateStore(cfg.cache_path / "state.json")

    def frame_name(self, asset: dict) -> str:
        stem = Path(asset["originalFileName"]).stem
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:40] or "photo"
        return f"{self.cfg.file_prefix}{slug}_{asset['id'][:8]}.jpg"

    def _beat(self) -> None:
        self.heartbeat.touch()

    def _push(self, asset_id: str, name: str, jpeg: bytes) -> bool:
        try:
            self.frame.upload(name, jpeg)
        except FrameError as exc:
            log.warning("Upload of %s failed, will retry: %s", name, exc)
            self.state.set(asset_id, name, "pending")
            return False
        self.state.set(asset_id, name, "uploaded")
        return True

    def sync_once(self) -> dict:
        summary = {"added": 0, "retried": 0, "pending": 0, "removed": 0, "skipped": 0, "errors": 0}
        try:
            images = self.immich.album_images()
        except ImmichError as exc:
            # Never act on a failed fetch — an error must not look like an empty album.
            log.error("Could not read album, skipping this cycle: %s", exc)
            summary["errors"] += 1
            return summary
        album = {a["id"]: a for a in images}

        # 1. Retry uploads that failed while the frame was offline.
        for asset_id, entry in list(self.state.assets.items()):
            if self.stop.is_set():
                return summary
            if entry["status"] != "pending" or asset_id not in album:
                continue
            cached = self.processed_dir / entry["file"]
            if not cached.exists():
                self.state.drop(asset_id)  # cache was wiped; reprocess as a new asset below
                continue
            if self._push(asset_id, entry["file"], cached.read_bytes()):
                summary["retried"] += 1
            else:
                summary["pending"] += 1

        # 2. New assets.
        for asset_id, asset in album.items():
            if self.stop.is_set():
                return summary
            if asset_id in self.state.assets:
                continue
            name = self.frame_name(asset)
            try:
                data = self.immich.download_original(asset_id)
                jpeg = processor.render(data, self.cfg.frame_width, self.cfg.frame_height, self.cfg.jpeg_quality)
            except ImmichError as exc:
                log.warning("Download of %s failed, will retry: %s", asset["originalFileName"], exc)
                summary["errors"] += 1
                continue
            except processor.ProcessingError as exc:
                log.warning("Skipping %s: %s", asset["originalFileName"], exc)
                self.state.set(asset_id, None, "skipped")
                summary["skipped"] += 1
                continue
            _atomic_write(self.processed_dir / name, jpeg)
            if self._push(asset_id, name, jpeg):
                log.info("Added %s -> %s", asset["originalFileName"], name)
                summary["added"] += 1
            else:
                summary["pending"] += 1
            self._beat()

        # 3. Assets removed from the album.
        for asset_id, entry in list(self.state.assets.items()):
            if self.stop.is_set():
                return summary
            if asset_id in album:
                continue
            name = entry["file"]
            if name and self.cfg.delete_from_frame:
                try:
                    self.frame.delete(name)
                except FrameError as exc:
                    log.warning("Delete of %s failed, will retry: %s", name, exc)
                    summary["errors"] += 1
                    continue
                log.info("Removed %s from frame", name)
            if name:
                (self.processed_dir / name).unlink(missing_ok=True)
            self.state.drop(asset_id)
            summary["removed"] += 1

        self._beat()
        return summary


# --- entrypoint -----------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run a single sync cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = Config.from_env()
    cfg.cache_path.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    immich = ImmichClient(cfg.immich_url, cfg.immich_api_key, cfg.immich_album_id)
    frame = FrameClient(cfg.frame_url, cfg.frame_user, cfg.frame_pass, cfg.frame_webdav_path)
    engine = SyncEngine(cfg, immich, frame, stop)

    log.info(
        "Album %s -> %s (%dx%d), every %d min, delete from frame: %s",
        cfg.immich_album_id, cfg.frame_url, cfg.frame_width, cfg.frame_height,
        cfg.interval_minutes, "on" if cfg.delete_from_frame else "off",
    )
    if not frame.ping():
        log.warning("Frame %s is not reachable right now; uploads will be retried", cfg.frame_url)

    try:
        while not stop.is_set():
            summary = engine.sync_once()
            log.info("Cycle done: %s", ", ".join(f"{k}={v}" for k, v in summary.items()))
            if args.once:
                break
            stop.wait(cfg.interval_minutes * 60)
    finally:
        immich.close()
        frame.close()
        log.info("Stopped")


if __name__ == "__main__":
    main()
