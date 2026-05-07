# ch340bcfg

A Python command-line application for Linux to read and write the configuration EEPROM on WCH CH340B USB-to-UART chips. Supports setting the USB Vendor ID, Product ID, serial number, and product string — the same fields the official Windows `CH340BConfigure.exe` tool can program, but without needing Windows or Wine.

Includes a wrapper script (`program-batch`) for production use that maintains a persistent serial-number counter across runs, so you can program a tray of chips, plug in another tray, and pick up where you left off automatically.

## Why

The CH340B is a cheap and widely-used USB-UART bridge chip, and unlike its predecessor the CH340G, it has internal EEPROM that lets you customize the USB descriptors — useful when you're shipping product and want it to enumerate as something more identifiable than "USB2.0-Serial".

WCH ships a Windows configuration tool, and there's a community .NET reimplementation by [@senthilnathant](https://github.com/senthilnathant/tools-ch340b-configuration), but neither runs natively on Linux. This tool fills that gap.

## Requirements

- Linux (tested on Ubuntu 24.04 LTS)
- Python 3.10+
- `pyusb` (`pip install -r requirements.txt`, or `pip install pyusb`)
- `libusb-1.0` (`apt install libusb-1.0-0` on Debian/Ubuntu, usually already installed)

## Installation

```bash
git clone https://github.com/tube0013/ch340bcfg.git
cd ch340bcfg
chmod +x ch340bcfg.py program-batch

# Optional: put it on your $PATH
cp ch340bcfg.py program-batch ~/bin/
```

### Run without sudo (recommended)

By default libusb access requires root. To run as a normal user, install a udev rule:

```bash
sudo tee /etc/udev/rules.d/99-ch340b.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb
```

Make sure your user is in the `plugdev` group (default on Ubuntu desktop installs):

```bash
groups | grep plugdev || sudo usermod -aG plugdev $USER
# log out and back in if you had to add yourself
```

## Usage

### `list` — show all attached CH340 chips

```
$ ch340bcfg.py list
Found 2 CH340-family device(s):
  bus 001 address 033  (--bus 1 --address 33)
  bus 001 address 034  (--bus 1 --address 34)
```

### `read` — read the EEPROM contents

Single device:

```
$ ch340bcfg.py read
Reading from bus 1 address 33 ...
  VID:            0x1A86
  PID:            0x7523
  Serial number:  'TZB00101'
  Product string: 'TubesZB-USB-C'
```

All attached devices:

```
$ ch340bcfg.py read --all
Reading 2 device(s) ...

[1/2] bus 1 address 33
  VID:            0x1A86
  PID:            0x7523
  Serial number:  'TZB00101'
  Product string: 'TubesZB-USB-C'

[2/2] bus 1 address 34
  VID:            0x1A86
  PID:            0x7523
  Serial number:  'TZB00102'
  Product string: 'TubesZB-USB-C'
```

If you have multiple chips plugged in and want to read just one, pass `--bus N --address M` from the `list` output.

### `write` — program the EEPROM

Any combination of the four fields can be written; unspecified fields are left unchanged.

```
ch340bcfg.py write [--bus N --address M | --all]
                   [--vid 0x1A86] [--pid 0x7523]
                   [--serial SN001234]
                   [--product "My Widget v1"]
                   [--serial-start N]
                   [--dry-run] [--yes]
```

Single device:

```
$ ch340bcfg.py write --serial "TZB00103" --product "TubesZB-USB-C"
Opening bus 1 address 33 ...

Current configuration:
  VID:            0x1A86
  PID:            0x7523
  Serial number:  ''
  Product string: ''

Planned configuration:
  VID:            0x1A86
  PID:            0x7523
  Serial number:  'TZB00103'
  Product string: 'TubesZB-USB-C'

Write these values to the chip? [y/N] y

Writing ...

Readback:
  VID:            0x1A86
  PID:            0x7523
  Serial number:  'TZB00103'
  Product string: 'TubesZB-USB-C'

Unplug and replug the device for new descriptors to take effect.
```

Bulk write with auto-incrementing serials. The `{n}` token in `--serial` gets substituted per device; standard Python format-spec syntax applies, so `{n:05d}` zero-pads to 5 digits:

```
$ ch340bcfg.py write --all \
    --product "TubesZB-USB-C" \
    --serial "TZB{n:05d}" \
    --serial-start 100

Plan: write 2 device(s)
  [1/2] bus 1 address 33  serial='TZB00100'  product='TubesZB-USB-C'
  [2/2] bus 1 address 34  serial='TZB00101'  product='TubesZB-USB-C'

Proceed with batch write? [y/N] y
...
Done: 2/2 succeeded.
Unplug and replug devices for new descriptors to take effect.
```

Devices are sorted by `(bus, address)` for stable ordering.

### Field limits

| Field        | Limit                                     |
|--------------|-------------------------------------------|
| `--vid`      | 16-bit unsigned (decimal or `0x`-prefixed)|
| `--pid`      | 16-bit unsigned (decimal or `0x`-prefixed)|
| `--serial`   | 8 ASCII characters                        |
| `--product`  | 18 characters (stored as UTF-16LE)        |

The script validates these up-front and refuses to write rendered serials that exceed 8 chars (e.g. `{n:09d}` would error before touching any chip).

### Useful flags

- `--dry-run` — show the plan without writing. Works in single-device and `--all` modes.
- `--yes` / `-y` — skip the confirmation prompt. Required when scripting.
- `--bus N` `--address M` — target one specific chip when multiple are plugged in.

## VID/PID changes

The in-tree Linux `ch341` kernel driver matches strictly on `1a86:7523`. If you change the VID or PID, the chip will stop appearing as `/dev/ttyUSB*` after the next plug cycle. To make the driver bind to your custom IDs:

```bash
echo XXXX YYYY | sudo tee /sys/bus/usb-serial/drivers/ch341-uart/new_id
```

(Replace `XXXX YYYY` with your hex VID and PID.) The script prints this exact command at the end of any write that changes VID or PID. Note that this is per-boot — for permanent binding you'd need a systemd unit, modprobe option, or your own kernel driver.

## Production batches: `program-batch`

The `program-batch` wrapper handles the bookkeeping for repeated runs: it stores the next serial number in a small state file, programs every chip currently plugged in, and advances the counter only by the number that succeeded. Plug in a tray, run it, unplug, repeat.

Edit the four configuration lines at the top of the script:

```bash
PRODUCT="TubesZB-USB-C"
TEMPLATE="TZB{n:05d}"
STATE_FILE="${HOME}/.ch340b/tubeszb.counter"
LOG_FILE="${HOME}/.ch340b/tubeszb.log"
```

Then to start a counter at a specific value:

```bash
mkdir -p ~/.ch340b
echo 100 > ~/.ch340b/tubeszb.counter
```

Daily use:

```
$ program-batch
About to program 4 chip(s):
  product:  TubesZB-USB-C
  serials:  TZB00103..TZB00106  (counter starts at 103)
  state:    /home/byron/.ch340b/tubeszb.counter

[... ch340bcfg.py output ...]

Done: 4/4 succeeded.
Programmed 4 chip(s). Counter advanced to 107.
```

Features:

- **Counter only advances on success.** If 3 of 4 chips program successfully, the counter advances by 3, not 4. The failed chip's serial is retained for the next run.
- **Total failure leaves the counter alone.** Yank the cable mid-run? Re-run and you're back where you were.
- **Audit log** appended to `~/.ch340b/tubeszb.log` — every chip ever programmed is searchable by serial number.
- **`--dry-run`** shows the plan and exits without advancing the counter.
- **Per-SKU counters** via `--state /path/to/other-sku.counter`.

Multi-SKU example:

```bash
program-batch --product "TubesZB-Coordinator" \
              --template "COORD{n:04d}" \
              --state ~/.ch340b/coord.counter
```

## Protocol

The CH340B exposes its EEPROM via two vendor-class USB control transfers on endpoint 0:

```
Write byte:    bmRequestType=0x40  bRequest=0x54  wValue=(addr<<8)|val  wIndex=0xA001  wLen=0
Settle pulse:  bmRequestType=0x40  bRequest=0x5E  wValue=0x000A          wIndex=0x0000  wLen=0
Read byte:     bmRequestType=0xC0  bRequest=0x54  wValue=(addr<<8)       wIndex=0xA001  wLen=1   → 1 byte
```

The settle pulse fires after every single byte written by the official tool — this is the EEPROM commit/wait, and skipping it risks bytes not committing before the next write.

EEPROM layout:

| Address     | Field                                                         |
|-------------|---------------------------------------------------------------|
| `0x00`      | Magic byte `0x5B` (validates the configuration)               |
| `0x04–0x05` | VID, little-endian                                            |
| `0x06–0x07` | PID, little-endian                                            |
| `0x10–0x17` | Serial number, 8 ASCII bytes (zero-padded)                    |
| `0x1A`      | Product-string descriptor length (UTF-16 byte count + 2)      |
| `0x1B`      | USB string descriptor type (`0x03`)                           |
| `0x1C–0x3F` | Product string, UTF-16LE (36 bytes = 18 characters max)       |

## How this was developed

This tool was developed iteratively in conversation with [Claude](https://claude.ai/), Anthropic's AI assistant. The interesting part is *how* — because the WCH protocol is undocumented, the development arc looked more like reverse engineering than coding.

**Round 1: source inspection.** I provided the `.cs` source of the senthilnathant .NET tool. Claude initially read it as a serial-port protocol — `WriteFile`/`ReadFile` calls with a `0x40` magic prefix — and produced a pyserial-based first draft. That draft did not work.

**Round 2: hypothesis revision.** A re-read of the C# source surfaced a comment indicating the WCH Windows kernel driver was translating those serial writes into USB control transfers on endpoint 0. Claude rewrote the tool to use libusb (pyusb), guessing at the wValue/wIndex layout based on the .NET tool's 4-byte command buffer. That draft also did not work — reads returned zero bytes.

**Round 3: probe and verify.** Claude added a `diag` subcommand that tried seven plausible variants of the read protocol (different bRequest values, different wValue/wIndex layouts, control-then-bulk patterns, and a try-after-init variant). All seven failed. This was useful negative information: it ruled out the entire family of "the .NET 4-byte buffer is the SETUP packet" theories.

**Round 4: capture the wire.** I set up VirtualBox with USB passthrough, ran the official .NET tool inside Windows, and captured the actual USB traffic on the Linux host with `usbmon` and `tshark`. I uploaded the `.pcapng` file. Claude wrote a Python parser for the pcapng + Linux usbmon binary formats (no `tshark` was available in its sandbox) and decoded all 228 vendor control transfers from the capture. This revealed the real protocol immediately:

- The actual USB bRequest is `0x54`, not `0xA0`/`0xA1`.
- The address is encoded in the high byte of `wValue`, not the low byte.
- `wIndex` is a constant `0xA001` selecting the EEPROM function.
- Every write is followed by a settle/commit pulse using bRequest `0x5E`.

The .NET tool's 4-byte command buffer was indeed a private encoding inside the WCH Windows kernel driver, not a literal SETUP packet. Without the USB capture, this would have been very hard to figure out.

**Round 5: features and ergonomics.** Once the protocol was solid, additional rounds added the `--all` multi-device flag, the `{n}` serial template substitution with `--serial-start`, and the `program-batch` wrapper for persistent counters across production runs.

The whole development cycle took several conversational rounds across about a day of elapsed time. The total amount of code I personally wrote was zero — but I did do the USB capture, the iterative testing on real hardware, and the reality-checking when AI-generated code didn't work. AI was a force multiplier on protocol reverse engineering: it could read .NET source, propose hypotheses, write parsers for binary capture formats, and translate findings into working code, but it needed a human in the loop with the actual hardware to confirm what was real and what was hallucinated.

## Limitations

- **The chip must already be enumerable.** If a previous write set bad descriptors that confuse libusb enumeration, you may not be able to recover without external means. (The chip's hardware ID match is fixed in silicon, but its descriptor-parsing logic is firmware.)
- **Only the CH340B is supported.** Other CH340 variants (CH340G, CH340N, CH340E) and related chips (CH341, CH343, CH9102) have different EEPROM access protocols. This tool is specific to the CH340B.
- **Descriptors only refresh on plug cycle.** After a successful write, the chip continues to enumerate with its old USB descriptors until you unplug and replug. The tool's readback uses the EEPROM-read protocol directly, so you can verify writes immediately, but `lsusb` will show stale info.
- **Tested on Ubuntu 24.04 LTS only.** Should work on any modern Linux with libusb-1.0 and Python 3.10+, but other distros may need different group names (`uucp` instead of `plugdev` on Arch, etc.) in the udev rule.

## Credits

- [@senthilnathant](https://github.com/senthilnathant/tools-ch340b-configuration) for the open-source .NET reference implementation that made the original protocol analysis possible.
- WCH (江苏沁恒股份有限公司) for the chip itself.
- [Claude](https://claude.ai/) (Anthropic) for development assistance.

## License

MIT — see [LICENSE](LICENSE) for the full text.
