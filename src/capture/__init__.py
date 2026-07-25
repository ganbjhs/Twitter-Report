"""Capture dispatcher (X-only).

    from capture import capture
    res = capture(page, url, shot_path)     # screenshots one X post

x_capture.capture(page, url, shot_path) -> result dict
(status / handle / screenshot / text). The dispatcher just stamps the platform.
"""
from . import x_capture


def capture(page, url, shot_path, platform: str = "x") -> dict:
    result = x_capture.capture(page, url, shot_path)
    result["platform"] = "x"
    return result
