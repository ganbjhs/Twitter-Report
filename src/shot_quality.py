"""Decide whether a tweet screenshot actually captured the post, or came out
black / blank / half-loaded / still behind a spinner.

The signal is pixel *variance*: a real tweet always has a light header band with
dark text (and usually media), so it has plenty of contrast. A failed capture —
an all-black frame, a blank white frame, or a lone loading spinner — is nearly
uniform, so its standard deviation collapses toward zero. We pair that with a
size floor and a "very dark overall" check.

    good, reason = screenshot_quality(path)   # good=False  -> recapture it
"""


def screenshot_quality(path):
    try:
        from PIL import Image, ImageStat
    except Exception:
        return True, "pil-missing"        # can't analyze -> don't block

    try:
        im = Image.open(path).convert("L")
    except Exception:
        return False, "unreadable"

    w, h = im.size
    if h < 180 or w < 150:
        return False, f"too-small {w}x{h}"

    stat = ImageStat.Stat(im)
    mean, std = stat.mean[0], stat.stddev[0]

    # near-uniform frame = blank / solid-black / solid-white / spinner-on-blank
    if std < 8:
        return False, f"blank-or-uniform (std={std:.1f})"
    # very dark overall with little structure = black / unrendered media
    if mean < 25 and std < 18:
        return False, f"too-dark (mean={mean:.0f}, std={std:.1f})"

    return True, "ok"
