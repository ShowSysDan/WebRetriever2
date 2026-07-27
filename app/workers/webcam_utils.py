"""
Webcam detection via V4L2 (Linux).

Enumerates /dev/video* nodes and filters them down to actual capture
devices using the VIDIOC_QUERYCAP ioctl. This filtering matters: modern
UVC cameras register two device nodes each (one video capture, one
metadata), so a naive /dev/video* listing shows every camera twice.

Pure stdlib — no OpenCV or v4l2 bindings needed just to enumerate.
"""

import os
import glob
import struct
import logging
import platform

logger = logging.getLogger(__name__)

# ioctl request code for VIDIOC_QUERYCAP (_IOR('V', 0, struct v4l2_capability))
_VIDIOC_QUERYCAP = 0x80685600

# struct v4l2_capability layout (104 bytes):
#   __u8 driver[16]; __u8 card[32]; __u8 bus_info[32];
#   __u32 version; __u32 capabilities; __u32 device_caps; __u32 reserved[3];
_V4L2_CAPABILITY_FMT = "16s32s32sIII12x"

_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_DEVICE_CAPS = 0x80000000


def _decode(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _query_device(path: str) -> dict | None:
    """Return device info if `path` is a video capture device, else None."""
    import fcntl

    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(struct.calcsize(_V4L2_CAPABILITY_FMT))
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, buf)
        driver, card, bus_info, version, caps, device_caps = struct.unpack(
            _V4L2_CAPABILITY_FMT, buf
        )
        # device_caps describes this specific node; capabilities describes
        # the whole physical device. Prefer the former when the driver sets it,
        # otherwise metadata nodes would pass the capture check.
        effective = device_caps if caps & _V4L2_CAP_DEVICE_CAPS else caps
        if not effective & _V4L2_CAP_VIDEO_CAPTURE:
            return None
        return {
            "device": path,
            "name": _decode(card),
            "driver": _decode(driver),
            "bus_info": _decode(bus_info),
        }
    except OSError:
        return None
    finally:
        os.close(fd)


def detect_webcams() -> list[dict]:
    """List connected video capture devices, e.g.
    [{"device": "/dev/video0", "name": "Logitech BRIO", ...}, ...]

    Returns [] on non-Linux platforms.
    """
    if platform.system() != "Linux":
        logger.info("Webcam detection is only supported on Linux (V4L2)")
        return []

    def node_index(p):
        digits = "".join(ch for ch in os.path.basename(p) if ch.isdigit())
        return int(digits) if digits else 0

    cams = []
    for path in sorted(glob.glob("/dev/video*"), key=node_index):
        info = _query_device(path)
        if info:
            cams.append(info)
    return cams
