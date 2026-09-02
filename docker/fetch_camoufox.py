"""Fetch the Camoufox archive in bounded HTTP range requests."""

from __future__ import annotations

import re
import time
from typing import Any

import requests
from camoufox.__main__ import CamoufoxUpdate, _do_sync


CHUNK_SIZE = 32 * 1024 * 1024
REQUEST_TIMEOUT = (15, 60)
REQUEST_ATTEMPTS = 4
CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


def _content_range(response: requests.Response) -> tuple[int, int, int]:
    match = CONTENT_RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
    if response.status_code != 206 or match is None:
        raise RuntimeError("Camoufox archive endpoint did not honor byte ranges")
    return tuple(int(value) for value in match.groups())


class ChunkedCamoufoxUpdate(CamoufoxUpdate):
    @staticmethod
    def download_file(file: Any, url: str) -> Any:
        file.seek(0)
        file.truncate(0)

        with requests.get(
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            _, _, total = _content_range(response)

        offset = 0
        while offset < total:
            requested_end = min(offset + CHUNK_SIZE - 1, total - 1)
            for attempt in range(1, REQUEST_ATTEMPTS + 1):
                try:
                    file.seek(offset)
                    file.truncate()
                    with requests.get(
                        url,
                        headers={"Range": f"bytes={offset}-{requested_end}"},
                        stream=True,
                        timeout=REQUEST_TIMEOUT,
                    ) as response:
                        range_start, range_end, range_total = _content_range(response)
                        if (
                            range_start != offset
                            or range_total != total
                            or range_end < range_start
                        ):
                            raise RuntimeError("Camoufox archive returned an invalid byte range")
                        expected = range_end - range_start + 1
                        received = 0
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                file.write(chunk)
                                received += len(chunk)
                        if received != expected:
                            raise RuntimeError(
                                f"Camoufox archive range truncated ({received}/{expected} bytes)"
                            )
                        offset = range_end + 1
                        print(
                            f"Camoufox archive: {offset}/{total} bytes",
                            flush=True,
                        )
                        break
                except Exception:
                    if attempt == REQUEST_ATTEMPTS:
                        raise
                    time.sleep(attempt)
            else:
                raise RuntimeError("Camoufox archive range download failed")

        file.flush()
        file.seek(0)
        return file


def main() -> None:
    _do_sync()
    update = ChunkedCamoufoxUpdate()
    update.update(i_know_what_im_doing=True)


if __name__ == "__main__":
    main()
