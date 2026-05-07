#!/usr/bin/env python3
"""
ch340bcfg — Linux command-line tool to read/write the configuration EEPROM
on WCH CH340B USB-UART chips (VID/PID, USB serial number, USB product string).

Protocol verified by USB capture of CH340BConfigure.exe v1.0.0 (the senthilnathant
.NET tool) running in a Windows VM, sniffed via Linux usbmon. Every read or write
of one EEPROM byte is a single vendor control transfer:

    Write byte:  bmRequestType=0x40  bRequest=0x54
                 wValue = (addr << 8) | val   wIndex=0xA001  wLength=0
                 (Followed by a settle pulse: 0x40 0x5E wValue=0x000A wIndex=0)
    Read byte:   bmRequestType=0xC0  bRequest=0x54
                 wValue = (addr << 8)         wIndex=0xA001  wLength=1   → 1 byte

Requirements:
    pip install pyusb       (and a working libusb-1.0 on the system)

Permissions:
    libusb access usually needs root, OR a udev rule. To run unprivileged,
    drop a file at /etc/udev/rules.d/99-ch340b.rules with:

        SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", \\
            MODE="0660", GROUP="plugdev", TAG+="uaccess"

    then `sudo udevadm control --reload-rules && sudo udevadm trigger`
    and re-plug the device. (Substitute 'dialout' for 'plugdev' on Debian
    if your user is in dialout instead.)

    The script automatically detaches and re-attaches the ch341 kernel
    driver so /dev/ttyUSB0 will be available again when you're done.

Usage:
    ch340bcfg list
    ch340bcfg read  [--bus N --address M] [--all]
    ch340bcfg write [--bus N --address M] [--all]
                    [--vid 0x1A86] [--pid 0x7523]
                    [--serial SN001234] [--product "My Widget v1"]
                    [--serial-start N]      (counter for {n} in --serial)
                    [--dry-run] [--yes]

Limits enforced by the chip / spec:
    --serial   : 8 ASCII characters maximum (zero-padded)
                 may contain a {n} or {n:NNd} token that auto-increments
                 across devices when --all is used
    --product  : 18 characters maximum (stored as UTF-16LE, 36 bytes)
    --vid/--pid: 16-bit unsigned integers (decimal or 0x-prefixed hex)

Bulk operations:
    ch340bcfg read --all                    # show every attached chip
    ch340bcfg write --all --product "Widget" --serial "WGT{n:04d}" --serial-start 1
                                            # → WGT0001, WGT0002, ... in (bus,addr) order

Note: writing a non-default VID/PID will stop the in-tree ch341 driver from
binding to the device on next plug-in (it matches strictly on 1a86:7523).
The script prints the `new_id` sysfs incantation needed to re-bind.
"""

import argparse
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.stderr.write("error: pyusb is required. Install with: pip install pyusb\n")
    sys.stderr.write("       (also needs libusb-1.0 on the system, e.g. apt install libusb-1.0-0)\n")
    sys.exit(2)


# -- USB IDs the kernel ch341 driver claims --------------------------------
CH340_VID = 0x1A86
CH340_PID = 0x7523

# -- EEPROM map ------------------------------------------------------------
ADDR_MAGIC          = 0x00  # value 0x5B = "config valid"
VAL_MAGIC           = 0x5B
ADDR_VID_LSB        = 0x04
ADDR_VID_MSB        = 0x05
ADDR_PID_LSB        = 0x06
ADDR_PID_MSB        = 0x07
ADDR_SERIAL_START   = 0x10  # 8 bytes ASCII, 0x10..0x17 inclusive
ADDR_SERIAL_END     = 0x17
ADDR_PRODSTR_LEN    = 0x1A  # length-of-descriptor byte (UTF-16 byte count + 2)
ADDR_PRODSTR_TYPE   = 0x1B  # 0x03 = USB string descriptor type
ADDR_PRODSTR_DATA   = 0x1C  # 36 bytes UTF-16LE, 0x1C..0x3F inclusive
ADDR_PRODSTR_END    = 0x3F

SERIAL_MAX_CHARS    = 8
PRODUCT_MAX_CHARS   = 18
PRODUCT_BUF_BYTES   = 38  # 2-byte header + 36 bytes data

# -- USB control-transfer protocol -----------------------------------------
# Reverse-engineered from a USB capture of CH340BConfigure.exe v1.0.0 doing
# a Write+Read cycle (Windows running in VirtualBox, captured on Linux host
# via usbmon). Every byte written or read uses bRequest=0x54 and wIndex=0xA001;
# settle/commit pulses use bRequest=0x5E.
BM_VENDOR_OUT       = 0x40   # Vendor, Host->Device, recipient=Device
BM_VENDOR_IN        = 0xC0   # Vendor, Device->Host, recipient=Device
REQ_EEPROM          = 0x54   # both read and write
REQ_EEPROM_SETTLE   = 0x5E   # post-write commit/settle pulse
WIDX_EEPROM         = 0xA001 # constant; selects EEPROM function in the chip
WVAL_SETTLE         = 0x000A # constant payload for settle pulse

USB_TIMEOUT_MS      = 1000


# -- Device handling -------------------------------------------------------

def find_devices():
    """Return list of (usb.core.Device, bus, address) tuples for all CH340s."""
    return [
        (d, d.bus, d.address)
        for d in usb.core.find(find_all=True, idVendor=CH340_VID, idProduct=CH340_PID)
    ]


def pick_device(want_bus, want_addr):
    devs = find_devices()
    if not devs:
        sys.exit("error: no CH340-family device (1a86:7523) found. "
                 "Check `lsusb` and `dmesg | tail`.")
    if want_bus is not None or want_addr is not None:
        filtered = [d for (d, b, a) in devs
                    if (want_bus is None or b == want_bus)
                    and (want_addr is None or a == want_addr)]
        if not filtered:
            sys.exit(f"error: no CH340 matching bus={want_bus} address={want_addr}.")
        if len(filtered) > 1:
            sys.exit("error: multiple matches; tighten --bus/--address.")
        return filtered[0]
    if len(devs) > 1:
        listing = "\n".join(f"  --bus {b} --address {a}" for (_, b, a) in devs)
        sys.exit(f"error: multiple CH340 devices present, pass one of:\n"
                 f"{listing}\nor pass --all to operate on all of them.")
    return devs[0][0]


def pick_devices_all():
    """Return all attached CH340 devices, sorted by (bus, address) for stable order."""
    devs = find_devices()
    if not devs:
        sys.exit("error: no CH340-family device (1a86:7523) found. "
                 "Check `lsusb` and `dmesg | tail`.")
    devs.sort(key=lambda x: (x[1], x[2]))
    return [d for (d, _, _) in devs]


class CH340B:
    """Context manager: detaches kernel driver on enter, re-attaches on exit."""

    def __init__(self, dev):
        self.dev = dev
        self._reattach = False

    def __enter__(self):
        # ch341 kernel driver claims interface 0; we need to evict it before
        # libusb can do control transfers reliably on most kernels.
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                self._reattach = True
        except (usb.core.USBError, NotImplementedError):
            # Some platforms (e.g. *BSD) don't support this call; carry on.
            pass
        # We don't claim the interface — control endpoint 0 doesn't need it.
        return self

    def __exit__(self, *exc):
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        if self._reattach:
            try:
                self.dev.attach_kernel_driver(0)
            except Exception:
                pass

    def read_byte(self, addr):
        """Read one EEPROM byte. Verified against USB capture of CH340BConfigure.exe."""
        if not 0 <= addr <= 0xFF:
            raise ValueError(f"address out of range: 0x{addr:X}")
        # wValue: high byte = address, low byte = 0
        data = self.dev.ctrl_transfer(
            BM_VENDOR_IN, REQ_EEPROM,
            wValue=(addr << 8), wIndex=WIDX_EEPROM, data_or_wLength=1,
            timeout=USB_TIMEOUT_MS,
        )
        if len(data) != 1:
            raise IOError(f"short read at 0x{addr:02X}: got {len(data)} bytes")
        return int(data[0])

    def write_byte(self, addr, val):
        """Write one EEPROM byte. Verified against USB capture of CH340BConfigure.exe.

        Sends the write command followed by the settle/commit pulse the
        Windows tool sends after every single byte (bRequest=0x5E, wValue=0x000A).
        """
        if not 0 <= addr <= 0xFF:
            raise ValueError(f"address out of range: 0x{addr:X}")
        if not 0 <= val <= 0xFF:
            raise ValueError(f"value out of range: 0x{val:X}")
        # wValue: high byte = address, low byte = value
        self.dev.ctrl_transfer(
            BM_VENDOR_OUT, REQ_EEPROM,
            wValue=(addr << 8) | val, wIndex=WIDX_EEPROM, data_or_wLength=b"",
            timeout=USB_TIMEOUT_MS,
        )
        # Settle/commit pulse — the Windows tool issues this after every byte.
        self.dev.ctrl_transfer(
            BM_VENDOR_OUT, REQ_EEPROM_SETTLE,
            wValue=WVAL_SETTLE, wIndex=0x0000, data_or_wLength=b"",
            timeout=USB_TIMEOUT_MS,
        )


# -- High-level operations -------------------------------------------------


def read_all(c):
    vid = c.read_byte(ADDR_VID_LSB) | (c.read_byte(ADDR_VID_MSB) << 8)
    pid = c.read_byte(ADDR_PID_LSB) | (c.read_byte(ADDR_PID_MSB) << 8)

    sn_bytes = bytes(c.read_byte(a) for a in range(ADDR_SERIAL_START, ADDR_SERIAL_END + 1))
    if 0x21 < sn_bytes[0] < 0x7F:
        serial_str = sn_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip()
    else:
        serial_str = ""

    str_len = c.read_byte(ADDR_PRODSTR_LEN)
    str_type = c.read_byte(ADDR_PRODSTR_TYPE)
    if str_type == 0x03 and 2 < str_len <= PRODUCT_BUF_BYTES:
        data_len = (str_len - 2) & ~1  # round to even (UTF-16 code units)
        raw = bytes(
            c.read_byte(a)
            for a in range(ADDR_PRODSTR_DATA, ADDR_PRODSTR_DATA + data_len)
        )
        try:
            product_str = raw.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            product_str = ""
    else:
        product_str = ""

    return {"vid": vid, "pid": pid, "serial": serial_str, "product": product_str}


def write_changes(c, vid=None, pid=None, serial_str=None, product=None):
    # Magic header is always written first (matches the .NET tool exactly).
    c.write_byte(ADDR_MAGIC, VAL_MAGIC)

    if vid is not None:
        c.write_byte(ADDR_VID_LSB, vid & 0xFF)
        c.write_byte(ADDR_VID_MSB, (vid >> 8) & 0xFF)

    if pid is not None:
        c.write_byte(ADDR_PID_LSB, pid & 0xFF)
        c.write_byte(ADDR_PID_MSB, (pid >> 8) & 0xFF)

    if serial_str is not None:
        if len(serial_str) > SERIAL_MAX_CHARS:
            raise ValueError(f"--serial too long (max {SERIAL_MAX_CHARS} ASCII chars)")
        try:
            sn_bytes = serial_str.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("--serial must be pure ASCII")
        sn_bytes = sn_bytes.ljust(SERIAL_MAX_CHARS, b"\x00")
        for i, b in enumerate(sn_bytes):
            c.write_byte(ADDR_SERIAL_START + i, b)

    if product is not None:
        utf16 = product.encode("utf-16-le")
        if len(utf16) > PRODUCT_BUF_BYTES - 2:
            raise ValueError(f"--product too long (max {PRODUCT_MAX_CHARS} chars)")
        # USB string-descriptor framing: [bLength, bDescriptorType=0x03, ...payload...]
        buf = bytes([len(utf16) + 2, 0x03]) + utf16
        buf = buf.ljust(PRODUCT_BUF_BYTES, b"\x00")
        for i, b in enumerate(buf):
            c.write_byte(ADDR_PRODSTR_LEN + i, b)


# -- CLI -------------------------------------------------------------------

def parse_uint16(s):
    s = s.strip()
    base = 16 if s.lower().startswith("0x") else 10
    v = int(s, base)
    if not 0 <= v <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"value out of range for uint16: {s}")
    return v


def fmt_config(cfg):
    return (
        f"  VID:            0x{cfg['vid']:04X}\n"
        f"  PID:            0x{cfg['pid']:04X}\n"
        f"  Serial number:  {cfg['serial']!r}\n"
        f"  Product string: {cfg['product']!r}"
    )


def fmt_config_inline(cfg):
    """Single-line config summary for compact multi-device output."""
    return (f"VID=0x{cfg['vid']:04X} PID=0x{cfg['pid']:04X} "
            f"serial={cfg['serial']!r} product={cfg['product']!r}")



def cmd_list(args):
    devs = find_devices()
    if not devs:
        print("No CH340-family (1a86:7523) devices found.")
        return 1
    print(f"Found {len(devs)} CH340-family device(s):")
    for d, bus, addr in devs:
        print(f"  bus {bus:03d} address {addr:03d}  (--bus {bus} --address {addr})")
    return 0


def render_serial_template(template, n):
    """Substitute {n} / {n:NNd} tokens in a serial template.

    Falls back gracefully if the user passes a literal serial with no token —
    we just return it unchanged. We use str.format_map with a tolerant dict
    so non-{n} braces (unlikely in serials but possible) don't break things.
    """
    if template is None:
        return None
    if "{n" not in template:
        return template
    try:
        return template.format(n=n)
    except (KeyError, ValueError, IndexError) as e:
        sys.exit(f"error: bad --serial template {template!r}: {e}")


def _do_read_one(dev):
    """Open one device, read its config, return the dict."""
    with CH340B(dev) as c:
        return read_all(c)


def cmd_read(args):
    if args.all:
        devs = pick_devices_all()
        print(f"Reading {len(devs)} device(s) ...\n", file=sys.stderr)
        rc = 0
        for i, dev in enumerate(devs, 1):
            header = f"[{i}/{len(devs)}] bus {dev.bus} address {dev.address}"
            print(header)
            try:
                cfg = _do_read_one(dev)
                print(fmt_config(cfg))
            except (usb.core.USBError, IOError) as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                rc = 1
            print()
        return rc

    dev = pick_device(args.bus, args.address)
    print(f"Reading from bus {dev.bus} address {dev.address} ...", file=sys.stderr)
    cfg = _do_read_one(dev)
    print(fmt_config(cfg))
    return 0


def _do_write_one(dev, *, vid, pid, serial_str, product, dry_run, header):
    """Read-modify-write one device. Returns (rc, mismatches_list)."""
    print(header)
    try:
        with CH340B(dev) as c:
            before = read_all(c)
            print("  before: " + fmt_config_inline(before))

            planned = {
                "vid":     before["vid"]     if vid        is None else vid,
                "pid":     before["pid"]     if pid        is None else pid,
                "serial":  before["serial"]  if serial_str is None else serial_str,
                "product": before["product"] if product    is None else product,
            }
            print("  plan:   " + fmt_config_inline(planned))

            if dry_run:
                print("  [dry-run] no bytes written.")
                return (0, [], planned)

            write_changes(
                c,
                vid=vid,
                pid=pid,
                serial_str=serial_str,
                product=product,
            )
            time.sleep(0.1)
            after = read_all(c)
            print("  after:  " + fmt_config_inline(after))

        mismatches = [k for k in planned if planned[k] != after[k]]
        if mismatches:
            print(f"  WARNING: readback differs from planned for: "
                  f"{', '.join(mismatches)}", file=sys.stderr)
            return (2, mismatches, planned)
        return (0, [], planned)
    except (usb.core.USBError, IOError, ValueError) as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return (1, [], None)


def cmd_write(args):
    if all(x is None for x in (args.vid, args.pid, args.serial, args.product)):
        sys.exit("error: nothing to write. Specify at least one of "
                 "--vid / --pid / --serial / --product.")

    # Validate up-front: if --serial-start is given, --serial must contain {n}.
    if args.serial_start is not None and (args.serial is None or "{n" not in args.serial):
        sys.exit("error: --serial-start requires --serial to contain a {n} token "
                 "(e.g. --serial 'WGT{n:04d}')")

    # Multi-device path.
    if args.all:
        devs = pick_devices_all()
        if args.serial and "{n" not in args.serial and len(devs) > 1:
            sys.exit(
                f"error: --all selected {len(devs)} devices but --serial has no {{n}}\n"
                f"       token. All chips would get the same serial number, which\n"
                f"       defeats the point of unique serials. Use --serial 'PFX{{n:04d}}'\n"
                f"       (with a width that fits in 8 chars total), or write devices\n"
                f"       individually."
            )

        start = args.serial_start if args.serial_start is not None else 1
        # Pre-compute per-device serials so the dry-run/confirmation shows them.
        per_dev_serials = []
        for i, dev in enumerate(devs):
            s = render_serial_template(args.serial, start + i)
            if s is not None and len(s) > SERIAL_MAX_CHARS:
                sys.exit(f"error: rendered serial {s!r} is {len(s)} chars "
                         f"(max {SERIAL_MAX_CHARS}). Tighten the {{n:NNd}} width "
                         f"or shorten the prefix.")
            per_dev_serials.append(s)

        # Show plan and confirm once.
        print(f"Plan: write {len(devs)} device(s)")
        for i, dev in enumerate(devs, 1):
            prod_str = repr(args.product) if args.product else "(unchanged)"
            print(f"  [{i}/{len(devs)}] bus {dev.bus} address {dev.address}  "
                  f"serial={per_dev_serials[i-1]!r}  "
                  f"product={prod_str}")
        print()

        if not args.dry_run and not args.yes:
            resp = input("Proceed with batch write? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                print("Aborted.")
                return 1
            print()

        rc = 0
        succeeded = 0
        for i, dev in enumerate(devs, 1):
            header = f"[{i}/{len(devs)}] bus {dev.bus} address {dev.address}"
            r, _, _ = _do_write_one(
                dev,
                vid=args.vid, pid=args.pid,
                serial_str=per_dev_serials[i-1],
                product=args.product,
                dry_run=args.dry_run,
                header=header,
            )
            if r == 0:
                succeeded += 1
            else:
                rc = max(rc, r)
            print()

        print(f"Done: {succeeded}/{len(devs)} succeeded.")
        if not args.dry_run:
            print("Unplug and replug devices for new descriptors to take effect.")
        return rc

    # Single-device path.
    if args.serial and "{n" in args.serial:
        # Allow {n} in single-device mode too — it's not weird, just renders once.
        rendered = render_serial_template(args.serial,
                                          args.serial_start if args.serial_start is not None else 1)
        if len(rendered) > SERIAL_MAX_CHARS:
            sys.exit(f"error: rendered serial {rendered!r} is {len(rendered)} chars "
                     f"(max {SERIAL_MAX_CHARS}).")
        single_serial = rendered
    else:
        single_serial = args.serial

    dev = pick_device(args.bus, args.address)
    header = f"bus {dev.bus} address {dev.address}"

    # For single-device mode, do an explicit confirmation in the original style.
    print(f"Opening {header} ...", file=sys.stderr)
    with CH340B(dev) as c:
        before = read_all(c)
        print("\nCurrent configuration:")
        print(fmt_config(before))

        planned = {
            "vid":     before["vid"]     if args.vid       is None else args.vid,
            "pid":     before["pid"]     if args.pid       is None else args.pid,
            "serial":  before["serial"]  if single_serial  is None else single_serial,
            "product": before["product"] if args.product   is None else args.product,
        }
        print("\nPlanned configuration:")
        print(fmt_config(planned))

        if args.dry_run:
            print("\n[dry-run] no bytes written.")
            return 0

        if not args.yes:
            print()
            resp = input("Write these values to the chip? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                print("Aborted.")
                return 1

        print("\nWriting ...", file=sys.stderr)
        write_changes(
            c,
            vid=args.vid,
            pid=args.pid,
            serial_str=single_serial,
            product=args.product,
        )
        time.sleep(0.1)
        after = read_all(c)

    print("\nReadback:")
    print(fmt_config(after))

    mismatches = [k for k in planned if planned[k] != after[k]]
    if mismatches:
        print(f"\nWARNING: readback differs from planned for: {', '.join(mismatches)}",
              file=sys.stderr)
        return 2

    if planned["vid"] != CH340_VID or planned["pid"] != CH340_PID:
        print(
            "\nNote: VID/PID is no longer 1a86:7523. The new descriptors take\n"
            "effect after the next plug cycle. The Linux ch341 kernel driver\n"
            "matches strictly on 1a86:7523 — to make it bind to your custom IDs:\n"
            f"  echo {planned['vid']:04x} {planned['pid']:04x} | sudo tee \\\n"
            "       /sys/bus/usb-serial/drivers/ch341-uart/new_id",
            file=sys.stderr,
        )
    else:
        print("\nUnplug and replug the device for new descriptors to take effect.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="ch340bcfg",
        description="Read/write the configuration EEPROM on WCH CH340B chips.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list CH340-family devices on the system")\
        .set_defaults(func=cmd_list)

    def add_dev_args(sp):
        sp.add_argument("--bus", type=int, help="USB bus number (see `list`)")
        sp.add_argument("--address", type=int, help="USB device address (see `list`)")
        sp.add_argument("--all", action="store_true",
                        help="operate on all attached CH340-family devices")


    pr = sub.add_parser("read", help="read current configuration from chip")
    add_dev_args(pr)
    pr.set_defaults(func=cmd_read)

    pw = sub.add_parser("write", help="write configuration to chip")
    add_dev_args(pw)
    pw.add_argument("--vid", type=parse_uint16, help="USB Vendor ID (uint16)")
    pw.add_argument("--pid", type=parse_uint16, help="USB Product ID (uint16)")
    pw.add_argument("--serial",
                    help=f"USB serial number (<= {SERIAL_MAX_CHARS} ASCII chars). "
                         f"With --all, may contain a {{n}} or {{n:NNd}} token that "
                         f"auto-increments per device, e.g. 'WGT{{n:04d}}'.")
    pw.add_argument("--serial-start", type=int, default=None,
                    help="starting value for the {n} counter in --serial (default 1)")
    pw.add_argument("--product", help=f"USB product string (<= {PRODUCT_MAX_CHARS} chars)")
    pw.add_argument("--dry-run", action="store_true",
                    help="show what would be written but don't write")
    pw.add_argument("--yes", "-y", action="store_true",
                    help="skip confirmation prompt")
    pw.set_defaults(func=cmd_write)

    args = p.parse_args(argv)

    # Validate --all vs --bus/--address mutual exclusion (after parse so the
    # error message references the actual subcommand cleanly).
    if getattr(args, "all", False) and (args.bus is not None or args.address is not None):
        sys.exit("error: --all is mutually exclusive with --bus/--address")

    try:
        return args.func(args) or 0
    except usb.core.USBError as e:
        msg = str(e)
        if "Access denied" in msg or "Permission" in msg:
            sys.exit(f"USB error: {msg}\n"
                     "Try running as root, or install a udev rule (see --help).")
        sys.exit(f"USB error: {msg}")
    except (ValueError, IOError) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted.")


if __name__ == "__main__":
    sys.exit(main())
