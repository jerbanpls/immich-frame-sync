"""WebDAV client for the sk.mimac.slideshow app running on the picture frame."""

from urllib.parse import quote

import httpx


class FrameError(Exception):
    pass


class FrameClient:
    def __init__(self, base_url: str, user: str, password: str, webdav_path: str = "/webdav/"):
        self.base_url = base_url.rstrip("/")
        self.dav_url = f"{self.base_url}/{webdav_path.strip('/')}/"
        self._client = httpx.Client(
            auth=httpx.BasicAuth(user, password),
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    def close(self) -> None:
        self._client.close()

    def _url(self, name: str) -> str:
        return self.dav_url + quote(name)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise FrameError(f"Frame unreachable ({type(exc).__name__})") from exc
        if resp.status_code == 401:
            raise FrameError("Frame rejected credentials (check FRAME_USER / FRAME_PASS)")
        return resp

    def ping(self) -> bool:
        try:
            self._client.get(self.base_url, timeout=3.0)
            return True
        except httpx.HTTPError:
            return False

    def upload(self, name: str, data: bytes, content_type: str = "image/jpeg") -> None:
        resp = self._request("PUT", self._url(name), content=data, headers={"Content-Type": content_type})
        if resp.status_code not in (200, 201, 204):
            raise FrameError(f"Upload of {name} failed: HTTP {resp.status_code}")

    def delete(self, name: str) -> None:
        resp = self._request("DELETE", self._url(name))
        if resp.status_code not in (200, 204, 404):
            raise FrameError(f"Delete of {name} failed: HTTP {resp.status_code}")
