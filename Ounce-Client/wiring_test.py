"""
Ounce 4-slave wiring test.

Tests each slave slot in isolation by enabling exactly one at a time and
watching whether the master gets valid, advancing ACKs back from it. Testing
one slot at a time is what makes the result diagnostic: a fault then points at
that slave's own wiring rather than at the shared bus.

What a PASS proves for that slot:
  - CS   (master CS pin -> that slave's GP17) reaches the right board
  - SCK  (GP18) and MOSI (GP19 -> slave GP16) carry the command intact,
         because the slave only ACKs after a CRC-valid packet
  - MISO (that slave's GP19 -> its dedicated master pin) returns the reply
  - the slave is running the unified firmware and adopting the slot id

Run with nothing else talking to the port.
"""
import sys, time, struct, argparse, re
import serial
import serial.tools.list_ports

MISO_PINS = {0: "GP0", 1: "GP4", 2: "GP16", 3: "GP20"}
CS_PINS   = {0: "GP21", 1: "GP22", 2: "GP26", 3: "GP27"}

SPI_TARGET_ID_MASK = 0x03
SPI_ENABLED_SHIFT  = 2
SPI_ENABLED_MASK   = 0x3C


def crc8(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def packet(slot, enabled_mask):
    flags = ((slot & SPI_TARGET_ID_MASK)
             | ((enabled_mask << SPI_ENABLED_SHIFT) & SPI_ENABLED_MASK))
    p = struct.pack('<BBHBBBB', 0x5A, flags, 0, 128, 128, 128, 128)
    return p + bytes([crc8(p)])


def find_port():
    for p in serial.tools.list_ports.comports():
        if "2E8A" in (p.hwid or "").upper():
            return p.device
    return None


# "<< P +1234 !56 -0 -0"  -> per-slot flag and counter
P_LINE = re.compile(r"<< P ([+!-])(\d+) ([+!-])(\d+) ([+!-])(\d+) ([+!-])(\d+)")
ACK_LINE = re.compile(r"<< ACK: Target (\d) .*count=(\d+)")
# Raw bytes when a reply fails to validate. Hdr 0xFF across the board means the
# master saw only its own pull-up, i.e. nothing was driving that MISO line.
DIAG_LINE = re.compile(r"<< DIAG T(\d) Hdr:([0-9A-F]{2}) ID:([0-9A-F]{2})")


def test_slot(ser, slot, seconds):
    """Enable only `slot`, then watch its ACK flag and counter."""
    mask = 1 << slot
    pkt = packet(slot, mask)
    buf = b""
    samples = []          # (flag, counter)
    ack0 = []             # detailed line for the slot under test
    hdrs = []             # raw ACK header bytes seen on failures
    t0 = time.monotonic()
    last_w = 0.0
    # Let the master notice the new enabled mask before we start judging.
    settle_until = t0 + 0.4

    while time.monotonic() - t0 < seconds:
        now = time.monotonic()
        if now - last_w >= 0.002:
            last_w = now
            try:
                ser.write(pkt)
            except Exception:
                pass
        try:
            n = ser.in_waiting
        except Exception:
            break
        if n:
            buf += ser.read(n)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace")
                if now < settle_until:
                    continue
                m = P_LINE.search(line)
                if m:
                    g = m.groups()
                    samples.append((g[slot * 2], int(g[slot * 2 + 1])))
                    continue
                m2 = ACK_LINE.search(line)
                if m2 and int(m2.group(1)) == slot:
                    ack0.append(("+", int(m2.group(2))))
                    continue
                m3 = DIAG_LINE.search(line)
                if m3 and int(m3.group(1)) == slot:
                    hdrs.append(m3.group(2))
        time.sleep(0.001)

    return (ack0 + samples), hdrs


def verdict(slot, samples, hdrs=()):
    # An all-0xFF header means the master read nothing but its own pull-up on
    # that MISO pin, which separates a dead return path from a live-but-wrong
    # one. It does NOT by itself say whether CS or MISO is at fault: if CS
    # never reaches the slave it also never drives MISO. The slave's LED
    # settles that - it lights only when the slave receives valid packets.
    open_line = bool(hdrs) and all(h == "FF" for h in hdrs)
    if not samples:
        if open_line:
            return "FAIL", ("nothing driving MISO (master saw only its pull-up, Hdr=FF). "
                            "Check that slave's LED: lit = CS/SCK/MOSI fine so MISO is "
                            "the fault; dark = CS is not reaching it")
        return "NO DATA", "master never reported this slot (is the master flashed and running?)"
    flags = [f for f, _ in samples]
    counts = [c for _, c in samples]
    ok = flags.count("+")
    advancing = len(set(counts)) > 1 or (counts and counts[0] != 0)

    if ok == 0:
        if open_line:
            return "FAIL", ("no valid ACK; master saw only its pull-up (Hdr=FF), so nothing "
                            "is driving that MISO. Check that slave's LED: lit = MISO is the "
                            "fault; dark = CS is not reaching it")
        seen = ",".join(sorted(set(hdrs))[:4]) if hdrs else "none"
        return "FAIL", f"no valid ACK - slave replying with wrong data (headers seen: {seen})"
    if ok < len(flags) * 0.5:
        return "MARGINAL", f"only {ok}/{len(flags)} replies valid - intermittent link"
    if not advancing:
        return "MARGINAL", "ACKs valid but packet counter never advanced"
    return "PASS", f"{ok}/{len(flags)} valid, counter advancing"


def identify(ser, seconds):
    """Drive one slot at a time with an obvious, unmistakable input so you can
    see which physical controller answers.

    SPI slot order and USB enumeration order are unrelated: slot N means "the
    board on CS pin N", while Windows lists the four devices in whatever order
    it enumerated them. This is the only reliable way to learn which entry in
    joy.cpl corresponds to which slot."""
    print("Open joy.cpl and watch the four Switch Pro Controllers.\n"
          "Each slot in turn will slam its LEFT STICK FULL LEFT and hold\n"
          "the bottom face button (B). Note which one moves.\n")
    for slot in range(4):
        mask = 0xF                       # keep all enabled so none drop out
        # left stick hard left, B1 (bottom face button) held
        flags = ((slot & SPI_TARGET_ID_MASK)
                 | ((mask << SPI_ENABLED_SHIFT) & SPI_ENABLED_MASK))
        active = struct.pack('<BBHBBBB', 0x5A, flags, 1 << 4, 0, 128, 128, 128)
        active += bytes([crc8(active)])
        idle = {}
        for s in range(4):
            f = ((s & SPI_TARGET_ID_MASK)
                 | ((mask << SPI_ENABLED_SHIFT) & SPI_ENABLED_MASK))
            p = struct.pack('<BBHBBBB', 0x5A, f, 0, 128, 128, 128, 128)
            idle[s] = p + bytes([crc8(p)])

        print(f">>> SLOT {slot}  (CS {CS_PINS[slot]}, MISO {MISO_PINS[slot]}) "
              f"- stick LEFT + B held for {seconds:.0f}s", flush=True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            try:
                for s in range(4):
                    ser.write(active if s == slot else idle[s])
            except Exception:
                pass
            time.sleep(0.002)
        # settle everything back to neutral before the next slot
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.6:
            try:
                for s in range(4):
                    ser.write(idle[s])
            except Exception:
                pass
            time.sleep(0.002)
    print("\nDone. Note the joy.cpl order you observed - that is your "
          "slot -> player mapping.")


ID_LINE = re.compile(r"<< ID T(\d) ([0-9A-F]{16})")


def windows_usb_serials():
    """Serial numbers Windows currently sees for the emulated controllers.

    SDL is no help here: it reports an identical GUID for every Ounce board and
    exposes no serial at all, so the only way to tell the four apart on the host
    is to ask Windows directly."""
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity -Filter \"DeviceID LIKE '%VID_057E&PID_2009%'\""
             " | Select-Object -ExpandProperty DeviceID"],
            capture_output=True, text=True, timeout=25).stdout
    except Exception as e:
        print(f"[!] Could not query Windows devices: {e}")
        return {}
    serials = {}
    for line in out.splitlines():
        line = line.strip()
        if line.upper().startswith("USB\\"):
            serials[line.rsplit("\\", 1)[-1].upper()] = line
    return serials


def map_slots(ser, seconds):
    """Correlate each SPI slot with the physical board and its USB device."""
    mask = 0xF
    pkts = []
    for s in range(4):
        pkts.append(packet(s, mask))

    print(f"[+] Listening {seconds:.0f}s for board ids from the master...")
    found = {}
    buf = b""
    t0 = time.monotonic()
    last_w = 0.0
    while time.monotonic() - t0 < seconds and len(found) < 4:
        now = time.monotonic()
        if now - last_w >= 0.002:
            last_w = now
            for p in pkts:
                try:
                    ser.write(p)
                except Exception:
                    pass
        try:
            n = ser.in_waiting
        except Exception:
            break
        if n:
            buf += ser.read(n)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                m = ID_LINE.search(raw.decode("utf-8", "replace"))
                if m:
                    found[int(m.group(1))] = m.group(2)
        time.sleep(0.001)

    if not found:
        print("[-] No board ids reported. Is the master running the current "
              "firmware, and are the slaves responding?")
        return

    serials = windows_usb_serials()
    print("\n" + "=" * 72)
    print("SLOT -> PHYSICAL BOARD -> WINDOWS DEVICE")
    print("=" * 72)
    for slot in range(4):
        bid = found.get(slot)
        if not bid:
            print(f"  slot {slot}  (CS {CS_PINS[slot]:<5})  no response")
            continue
        dev = serials.get(bid)
        print(f"  slot {slot}  (CS {CS_PINS[slot]:<5})  board {bid}")
        print(f"           {'-> ' + dev if dev else '-> (not currently enumerated on this PC)'}")
    print("=" * 72)
    missing = [b for b in found.values() if b not in serials]
    if missing:
        print("\nSome boards did not appear as USB devices here - they are")
        print("presumably plugged into the Switch rather than this PC.")
    print("\nAssign inputs to a slot with:  --assign <slot>=pad:<name>")


def main():
    ap = argparse.ArgumentParser(description="Ounce 4-slave wiring test")
    ap.add_argument("--map", action="store_true",
                    help="Report which physical board and USB device each SPI slot "
                         "corresponds to, using the board ids the slaves report.")
    ap.add_argument("--identify", action="store_true",
                    help="Drive each slot in turn with an obvious input so you can "
                         "see which physical controller is which slot.")
    ap.add_argument("--port", default=None)
    ap.add_argument("--seconds", type=float, default=3.0,
                    help="seconds to test each slot (default 3)")
    ap.add_argument("--slots", default="0,1,2,3",
                    help="which slots to test, e.g. 0,1 (default all)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("[-] Could not find the master (VID 2E8A). Is it plugged in?")
        sys.exit(1)

    slots = [int(s) for s in args.slots.split(",") if s.strip() != ""]
    print(f"[+] Master on {port}")
    print(f"[+] Testing slots {slots}, {args.seconds:.0f}s each, one at a time.\n")

    try:
        ser = serial.Serial(port, 115200, timeout=0, write_timeout=0.05)
        ser.dtr = True
    except Exception as e:
        print(f"[-] Could not open {port}: {e}")
        sys.exit(1)

    if args.map:
        map_slots(ser, max(args.seconds, 8.0))
        ser.close()
        return

    if args.identify:
        identify(ser, args.seconds)
        ser.close()
        return

    results = {}
    for slot in slots:
        print(f"--- slot {slot}   (CS {CS_PINS[slot]}, MISO {MISO_PINS[slot]}) ---",
              flush=True)
        samples, hdrs = test_slot(ser, slot, args.seconds)
        state, why = verdict(slot, samples, hdrs)
        results[slot] = (state, why)
        print(f"    {state}: {why}\n", flush=True)

    ser.close()

    print("=" * 58)
    print("SUMMARY")
    for slot in slots:
        state, why = results[slot]
        print(f"  slot {slot}  {state:<9} {why}")
    print("=" * 58)

    bad = [s for s in slots if results[s][0] != "PASS"]
    if not bad:
        print("\nAll tested slots wired correctly.")
        return

    print("\nFor each failing slot, check in this order:")
    for s in bad:
        print(f"\n  slot {s}:")
        print(f"    1. CS   : master {CS_PINS[s]} -> that slave's GP17")
        print(f"    2. MISO : that slave's GP19 -> master {MISO_PINS[s]}")
        print(f"    3. MOSI : master GP19 -> that slave's GP16   (shared)")
        print(f"    4. SCK  : master GP18 -> that slave's GP18   (shared)")
        print(f"    5. GND shared between the boards, and slave powered")
        print(f"    6. slave running the unified firmware")
    working = [s for s in slots if results[s][0] == "PASS"]
    if working:
        print(f"\nSlots {working} pass, so SCK/MOSI/GND are good in general - "
              f"suspect the per-slave CS and MISO lines above first.")


if __name__ == "__main__":
    main()
