"""Capture dispatcher (X-only).

    from capture import capture
    res = capture(page, url, shot_path)     # screenshots one X post

x_capture.capture(page, url, shot_path) -> result dict
(status / handle / screenshot / text). The dispatcher just stamps the platform.

`keep_engagement` is passed straight through: False (the default) crops the
like/views line out, True keeps it — see x_capture's docstring.
"""
from . import x_capture


def capture(page, url, shot_path, platform: str = "x",
            keep_engagement: bool = False) -> dict:
    result = x_capture.capture(page, url, shot_path, keep_engagement)
    result["platform"] = "x"
    return result
