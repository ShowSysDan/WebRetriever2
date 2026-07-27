"""
Webcam detection via V4L2 (Linux).

Enumerates /dev/video* nodes and filters them down to actual capture
devices using the VIDIOC_QUERYCAP ioctl. This filtering matters: modern
UVC cameras register two device nodes each (one video capture, one
metadata), so a naive /dev/video* listing shows every camera twice.

Stable identity:
  /dev/videoN numbers are assigned by enumeration order and shuffle on
  replug/reboot. Each detected camera is therefore also resolved to a
  stable identifier so an instance stays bound to the correct camera:
    1. /dev/v4l/by-id/...   — udev symlink from USB vendor/model/serial;
                              follows the camera to any port
    2. /dev/v4l/by-path/... — udev symlink from the physical USB port;
                              fallback for cameras without a unique serial
  Instances should store the stable path; it resolves to the current
  /dev/videoN at open time.

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


def _stable_links(dirpath: str) -> dict:
    """Map real device path (/dev/videoN) -> stable udev symlink in dirpath.

    A camera usually has two links per directory (video-index0 for capture,
    video-index1 for metadata); keeping the first alphabetically keeps the
    capture node's link since we only look up capture devices.
    """
    links = {}
    if not os.path.isdir(dirpath):
        return links
    for entry in sorted(os.listdir(dirpath)):
        link = os.path.join(dirpath, entry)
        links.setdefault(os.path.realpath(link), link)
    return links


def _usb_attrs(video_node: str, sysfs_base: str = "/sys/class/video4linux") -> dict:
    """Read USB descriptor attributes (serial, product, manufacturer) for a
    /dev/videoN node by walking up its sysfs device chain to the USB device."""
    attrs = {}
    path = os.path.realpath(
        os.path.join(sysfs_base, os.path.basename(video_node), "device")
    )
    # The node's device is the USB *interface*; the descriptors live on the
    # USB *device* one or two levels up (marked by the idVendor attribute).
    for _ in range(4):
        if os.path.exists(os.path.join(path, "idVendor")):
            for key in ("serial", "product", "manufacturer", "idVendor", "idProduct"):
                try:
                    with open(os.path.join(path, key)) as f:
                        attrs[key] = f.read().strip()
                except OSError:
                    pass
            break
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return attrs


def detect_webcams() -> list[dict]:
    """List connected video capture devices, e.g.
    [{"device": "/dev/video0", "name": "Logitech BRIO",
      "stable_id": "/dev/v4l/by-id/usb-...-video-index0",
      "serial": "ABC123", ...}, ...]

    "stable_id" survives replugs and reboots (None if udev provides no link);
    "device" is the current, enumeration-order-dependent node.
    Returns [] on non-Linux platforms.
    """
    if platform.system() != "Linux":
        logger.info("Webcam detection is only supported on Linux (V4L2)")
        return []

    def node_index(p):
        digits = "".join(ch for ch in os.path.basename(p) if ch.isdigit())
        return int(digits) if digits else 0

    by_id = _stable_links("/dev/v4l/by-id")
    by_path = _stable_links("/dev/v4l/by-path")

    cams = []
    for path in sorted(glob.glob("/dev/video*"), key=node_index):
        info = _query_device(path)
        if not info:
            continue
        usb = _usb_attrs(path)
        # by-id encodes the serial number and follows the camera itself;
        # by-path encodes the physical port — fallback for serial-less cameras
        info["stable_id"] = by_id.get(path) or by_path.get(path)
        info["serial"] = usb.get("serial")
        cams.append(info)
    return cams
