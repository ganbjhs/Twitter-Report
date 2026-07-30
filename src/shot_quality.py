"""Decide whether a tweet screenshot actually captured the post, or came out
black / blank / half-loaded / still behind a spinner.

The signal is pixel *variance*: a real tweet always has a light header band with
dark text (and usually media), so it has plenty of contrast. A failed capture —
an all-black frame, a blank white frame, or a lone loading spinner — is nearly
uniform, so its standard deviation collapses toward zero. We pair that with a
size floor and a "very dark overall" check.

    good, reason = screenshot_quality(path)   # good=False  -> recapture it

A third failure mode joins those: a shot taken UNDER one of X's modal backdrops.
The dialog itself is caught deterministically in the DOM (see `overlays`), but a
dim layer we did not anticipate would otherwise pass every check above — it has
plenty of contrast, it is just uniformly darkened. The tell is the histogram: a
real tweet sits against a near-white (light theme) or near-black (dark theme)
background, so its pixels pile up at one END of the range. Multiply everything by
a ~0.4 backdrop and that pile lands in the MIDDLE instead, with nothing bright
left at all. That is what `_dimmed` looks for.
"""

# Fractions of a light-band / mid-band that mean "this was shot under a mask".
_BRIGHT = 200         # a real light-theme background sits well above this
_MID_LO, _MID_HI = 70, 150   # where a white background lands under a ~0.4 mask
_MID_SHARE = 0.55     # how much of the frame must be stuck in that band
_BRIGHT_SHARE = 0.05  # ...while this little of it is still bright


def _dimmed(hist, pixels):
    """(True, share) when the histogram looks uniformly darkened by an overlay."""
    if not pixels:
        return False, 0.0
    mid = sum(hist[_MID_LO:_MID_HI + 1]) / pixels
    bright = sum(hist[_BRIGHT:]) / pixels
    return (mid > _MID_SHARE and bright < _BRIGHT_SHARE), mid


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

    # shot through a modal backdrop: contrast survives, the whole frame is dim
    dimmed, share = _dimmed(im.histogram(), w * h)
    if dimmed:
        return False, f"dimmed-overlay ({share:.0%} mid-grey, nothing bright)"

    return True, "ok"
