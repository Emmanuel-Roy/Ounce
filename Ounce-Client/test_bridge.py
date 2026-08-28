import sys
import time
import struct
import argparse
import os
import re
import subprocess
import threading
import serial
import serial.tools.list_ports
import ctypes

# Windows Virtual Key Codes for Instant Momentary Keyboard Controls
VK_W = 0x57  # Up
VK_S = 0x53  # Down
VK_A = 0x41  # Left
VK_D = 0x44  # Right

VK_UP    = 0x26  # D-Pad Up
VK_DOWN  = 0x28  # D-Pad Down
VK_LEFT  = 0x25  # D-Pad Left
VK_RIGHT = 0x27  # D-Pad Right

VK_I = 0x49  # X (Top Face Button - SPI_MASK_B4)
VK_J = 0x4A  # Y (Left Face Button - SPI_MASK_B3)
VK_K = 0x4B  # B (Bottom Face Button - SPI_MASK_B1)
VK_L = 0x4C  # A (Right Face Button - SPI_MASK_B2)

VK_Y = 0x59  # ZL Trigger (L2)
VK_U = 0x55  # L Bumper (L1)
VK_O = 0x4F  # R Bumper (R1)
VK_P = 0x50  # ZR Trigger (R2)

VK_N = 0x4E  # Start (+) - SPI_MASK_S2
VK_M = 0x4D  # Select (-) - SPI_MASK_S1

VK_Z = 0x5A  # L3 Click
VK_X = 0x58  # R3 Click

VK_7 = 0x37  # Right Stick Left
VK_8 = 0x38  # Right Stick Up
VK_9 = 0x39  # Right Stick Right
VK_0 = 0x30  # Right Stick Down

VK_H = 0x48  # Home
VK_C = 0x43  # Capture

# SPI Button Mask Definitions matching GP2040-CE logical mapping
SPI_MASK_UP    = (1 << 0)
SPI_MASK_DOWN  = (1 << 1)
SPI_MASK_LEFT  = (1 << 2)
SPI_MASK_RIGHT = (1 << 3)

SPI_MASK_B1 = (1 << 4)  # B (Bottom)
SPI_MASK_B2 = (1 << 5)  # A (Right)
SPI_MASK_B3 = (1 << 6)  # Y (Left)
SPI_MASK_B4 = (1 << 7)  # X (Top)

SPI_MASK_L1 = (1 << 8)  # L
SPI_MASK_R1 = (1 << 9)  # R
SPI_MASK_L2 = (1 << 10) # ZL
SPI_MASK_R2 = (1 << 11) # ZR

SPI_MASK_S1 = (1 << 12) # Select (-)
SPI_MASK_S2 = (1 << 13) # Start (+)

SPI_MASK_L3 = (1 << 14) # L3
SPI_MASK_R3 = (1 << 15) # R3

# Home and Capture ride in the spare bits of the packet's flags byte, which
# also carries the 2-bit target id. The 16-bit buttons field above is full.
SPI_TARGET_ID_MASK   = 0x03
SPI_AUX_MASK_HOME    = (1 << 6)
SPI_AUX_MASK_CAPTURE = (1 << 7)

# Bits 2..5: which player slots the host has enabled, one bit per slot. The
# enabled set is arbitrary (e.g. only slots 1 and 3), so the master cannot
# infer it from which targets happen to have been addressed.
SPI_ENABLED_SHIFT    = 2
SPI_ENABLED_MASK     = 0x3C

def is_key_down(vk):
    return (ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000) != 0


NUM_SLOTS = 4          # the master dispatches to 4 target Picos
NEUTRAL = (0, 128, 128, 128, 128, 0)


def default_config():
    """Slot 0 driven by the built-in keyboard map plus the first controller;
    the rest disabled. Matches historical single-player behaviour."""
    return {
        "slots": {
            "0": {"keyboard": dict(DEFAULT_KEYBOARD_BINDINGS), "pad": None, "pad_buttons": {}},
            "1": {"keyboard": {}, "pad": None, "pad_buttons": {}},
            "2": {"keyboard": {}, "pad": None, "pad_buttons": {}},
            "3": {"keyboard": {}, "pad": None, "pad_buttons": {}},
        }
    }


def app_dir():
    """The folder the client is running from - next to the exe when frozen,
    next to the script when run from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def bundled_path(name):
    """A file that ships inside the build rather than beside it.

    PyInstaller unpacks --add-data files into sys._MEIPASS, which for an onedir
    build is the _internal\\ folder - not app_dir(), which is the folder holding
    the exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def recordings_root():
    """recordings\\ one level above the client.

    Above rather than beside: build_exe.bat runs PyInstaller with --noconfirm,
    which wipes the whole OunceClient folder on every rebuild. Keeping takes
    one level up means rebuilding the client cannot delete them.
    """
    return os.path.join(os.path.dirname(app_dir()), "recordings")


def new_recording_dir():
    """A fresh timestamped folder for one recording."""
    path = os.path.join(recordings_root(), time.strftime("%Y-%m-%d_%H-%M-%S"))
    os.makedirs(path, exist_ok=True)
    return path


def finish_recording_dir(path, seconds):
    """Append the length to the folder name, now that it is known.

    The name cannot carry the length when the folder is created, so the
    rename happens on stop. Returns wherever the recording ended up.
    """
    secs = int(round(seconds))
    length = f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"
    target = f"{path}_{length}"
    try:
        os.rename(path, target)
        return target
    except Exception as e:
        # Renaming can fail if anything still holds the folder open. The
        # recording itself is fine, so keep it under the original name.
        print(f"[-] Could not rename recording folder ({e}); left at {path}")
        return path


class InputRecorder:
    """Logs what each virtual controller was sent, one CSV per controller.

    Split per controller rather than one interleaved file because the point is
    to see what a given player did; a merged log would have to be demultiplexed
    before it could be read or plotted.

    Timestamps are milliseconds from the start of the recording, so they line
    up with the video in the same folder.
    """

    HEADER = "t_ms,buttons,lx,ly,rx,ry,aux\n"

    def __init__(self, directory):
        self.dir = directory
        self._files = {}
        self._t0 = time.monotonic()
        self.rows = 0

    def log(self, slot, buttons, lx, ly, rx, ry, aux):
        f = self._files.get(slot)
        if f is None:
            try:
                f = open(os.path.join(self.dir, f"controller{slot + 1}.csv"),
                         "w", encoding="utf-8", newline="")
            except Exception as e:
                print(f"[-] Cannot log controller {slot + 1}: {e}")
                self._files[slot] = False
                return
            f.write(self.HEADER)
            self._files[slot] = f
        elif f is False:                     # already failed; do not retry
            return
        f.write("%d,0x%04X,%d,%d,%d,%d,0x%02X\n" %
                (int((time.monotonic() - self._t0) * 1000),
                 buttons, lx, ly, rx, ry, aux))
        self.rows += 1

    def elapsed(self):
        return time.monotonic() - self._t0

    def close(self):
        for f in self._files.values():
            if f:
                try:
                    f.close()
                except Exception:
                    pass
        self._files.clear()


def keymap_store_path():
    """Where the in-app keyboard remapper persists its bindings.

    Deliberately NOT beside the exe: build_exe.bat runs PyInstaller with
    --noconfirm, which wipes bin/OunceClient/ on every rebuild, so a keymap
    kept there would be destroyed by rebuilding the client.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Ounce", "keymap.json")


def load_saved_keymap():
    """The saved bindings over the defaults, or just the defaults.

    Merged rather than replaced so that inputs added to the protocol later
    still get their default key instead of silently coming back unbound in
    everyone's saved file.
    """
    import json
    km = dict(DEFAULT_KEYBOARD_BINDINGS)
    path = keymap_store_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        return km
    except Exception as e:
        print(f"[-] Ignoring unreadable {path}: {e}")
        return km
    if not isinstance(saved, dict):
        print(f"[-] Ignoring {path}: expected an object of action -> key")
        return km
    bindings = saved.get("keyboard", saved)      # tolerate a bare action->key map
    if not isinstance(bindings, dict):
        print(f"[-] Ignoring {path}: 'keyboard' is not an object")
        return km
    # Filtered per entry rather than validated as a whole: one unrecognised
    # action - a hand edit, or a file written by a newer build - should not
    # throw away every other binding the player set.
    bad = []
    for action, key in bindings.items():
        name = str(key).upper()
        if action not in ALL_ACTIONS:
            bad.append(f"unknown input '{action}'")
        elif name not in VK_NAMES:
            bad.append(f"unknown key '{key}' for '{action}'")
        else:
            km[action] = name
    if bad:
        print("[-] Skipped in saved keymap: " + "; ".join(bad))

    # Rebinding a key in use unbinds whoever held it, and that absence has to
    # survive the restart. Merging over the defaults would otherwise hand the
    # key straight back to its default owner - so binding J to B1 would come
    # back as both B1 and B3 on J, the exact collision the remapper prevents.
    claimed = {km[a] for a in bindings if a in km}
    for action in list(km):
        if action not in bindings and km[action] in claimed:
            del km[action]

    print(f"[+] Keyboard bindings loaded from {path}")
    return km


def save_keymap(km):
    """Persist the live bindings. Never fatal - a remap that cannot be saved
    still applies to this session."""
    import json
    path = keymap_store_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"keyboard": km}, f, indent=2, sort_keys=True)
        os.replace(tmp, path)                    # never leave a half-written file
        return True
    except Exception as e:
        print(f"[-] Could not save keymap to {path}: {e}")
        return False


def load_config(path):
    import json
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "slots" not in cfg or not isinstance(cfg["slots"], dict):
        raise ValueError("config must contain a 'slots' object")

    errs = []
    for slot_s, sc in cfg["slots"].items():
        try:
            slot = int(slot_s)
        except ValueError:
            errs.append(f"slot key '{slot_s}' is not a number"); continue
        if not 0 <= slot < NUM_SLOTS:
            errs.append(f"slot {slot} out of range 0..{NUM_SLOTS - 1}")
        errs += validate_bindings(sc.get("keyboard", {}) or {},
                                  f"slot {slot} keyboard")
        errs += validate_bindings(sc.get("pad_buttons", {}) or {},
                                  f"slot {slot} pad_buttons", pad=True)
    if errs:
        raise ValueError("\n      ".join(errs))
    return cfg


def write_default_config(path):
    import json
    cfg = default_config()
    cfg["_comment"] = [
        "Ounce input mapping. One entry per virtual Switch controller slot.",
        "A slot is ENABLED if it has any keyboard binding or a pad assigned;",
        "the enabled set is sent to the master, and may be any subset -",
        "e.g. only slots 1 and 3.",
        "",
        "'keyboard' maps ACTION -> key name. Actions: " + " ".join(ALL_ACTIONS),
        "Axis actions are digital (LX- pushes left, LX+ right, etc).",
        "Key names: A-Z 0-9 UP DOWN LEFT RIGHT SPACE ENTER TAB SHIFT CTRL ALT",
        "           F1-F12 NUM0-NUM9 COMMA PERIOD SLASH etc.",
        "",
        "'pad' selects a controller by index or name substring, or null.",
        "'pad_buttons' overrides pad bindings; ACTION -> SDL button name",
        "           (A B X Y DPAD_UP LEFTSHOULDER BACK START GUIDE MISC1 ...).",
        "           Left empty, the positional defaults are used.",
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def probe_input(seconds=60.0):
    """Live dump of whatever a controller is actually sending.

    Useful under Steam Input, where what reaches us is whatever the Steam
    layout produces - not the physical device. If a thumbstick shows up in the
    HAT/D-pad row instead of the AXES row, the Steam layout has that stick
    bound to D-Pad rather than Joystick Move, and that has to be fixed in
    Steam's layout editor; nothing on this side can recover a stick's analog
    range once Steam has already reduced it to eight directions."""
    if not gamepad_available():
        return
    pads = list_real_pads()
    if not pads:
        # Fall back to raw joysticks: under Steam the virtual pad may not have
        # a GameController mapping yet, and we still want to see it.
        pads = [(i, pad_name(i))
                for i in range(_pg.joystick.get_count())
                if _pg.joystick.Joystick(i).get_name().strip().lower()
                not in OUNCE_SELF_NAMES]
    import os
    print("\n--- environment ---")
    for v in ("SteamAppId", "SteamGameId", "SteamClientLaunch",
              "SDL_JOYSTICK_HIDAPI_STEAM", "SDL_JOYSTICK_RAWINPUT",
              "SDL_VIDEODRIVER", "SDL_GAMECONTROLLER_IGNORE_DEVICES",
              "SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT"):
        print(f"   {v:42s} = {os.environ.get(v, '(unset)')}")
    print(f"   launched under Steam                       = "
          f"{'YES' if os.environ.get('SteamAppId') or os.environ.get('SteamGameId') else 'no'}")

    print("\n--- every joystick SDL can see ---")
    total = _pg.joystick.get_count()
    if total == 0:
        print("   (none at all)")
    for i in range(total):
        jj = _pg.joystick.Joystick(i)
        jj.init()
        tag = "  <-- Ounce target (ignored as input)" \
            if jj.get_name().strip().lower() in OUNCE_SELF_NAMES else ""
        # Shown name, then what SDL reported and the USB ids behind it: when a
        # pad is named wrongly, those two are what say why.
        vid, pid = pad_usb_ids(i)
        ids = f"{vid:04x}:{pid:04x}" if vid and pid else "no usb ids"
        print(f"   [{i}] {pad_name(i)}  axes={jj.get_numaxes()} "
              f"btn={jj.get_numbuttons()} hat={jj.get_numhats()} "
              f"mapped={_sdl_controller.is_controller(i)}{tag}")
        print(f"        sdl name: {jj.get_name()!r}  [{ids}]")

    if not pads:
        print("\n[-] No usable non-Ounce controller.")
        print("    If Steam shows a virtual gamepad but it is absent above, the")
        print("    process Steam hooked is not this one, or Steam is still")
        print("    applying its Desktop configuration.")
        return

    idx, name = pads[0]
    j = _pg.joystick.Joystick(idx)
    j.init()
    mapped = _sdl_controller.is_controller(idx)
    print(f"\n[+] Probing [{idx}] {name}")
    print(f"    axes={j.get_numaxes()} buttons={j.get_numbuttons()} hats={j.get_numhats()}")
    print(f"    SDL GameController mapping: {'yes' if mapped else 'NO'}")
    print("\n    Move the sticks. If stick motion appears under HATS instead of")
    print("    AXES, the Steam layout has it bound to D-Pad, not Joystick Move.")
    print("    Ctrl-C to stop.\n")

    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < seconds:
            _pg.event.pump()
            axes = [round(j.get_axis(i), 2) for i in range(j.get_numaxes())]
            hats = [j.get_hat(i) for i in range(j.get_numhats())]
            btns = [i for i in range(j.get_numbuttons()) if j.get_button(i)]
            sys.stdout.write("\r  AXES %-44s HATS %-14s BTN %-18s"
                             % (axes, hats, btns))
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    print("\n")


# --------------------------------------------------------------------------
# Pad naming.
#
# SDL's name for a pad is whatever the driver it came through reports, and
# XInput reports no product name at all - every pad reached that way is
# "XInput Controller #N" regardless of what is actually in your hands. The USB
# vendor and product ids ARE carried, packed into the SDL joystick GUID, so
# that is where the real identity has to come from.
# --------------------------------------------------------------------------

# Names that identify nothing. Matched as a prefix, so the "#1" that XInput
# appends is still caught.
GENERIC_PAD_NAMES = ("xinput controller", "controller", "gamepad",
                     "usb gamepad", "generic", "wireless controller")

# (vendor, product) -> what people actually call it.
PAD_NAMES = {
    (0x054C, 0x0CE6): "DualSense",
    (0x054C, 0x0DF2): "DualSense Edge",
    (0x054C, 0x09CC): "DualShock 4",
    (0x054C, 0x05C4): "DualShock 4",
    (0x054C, 0x0BA0): "DualShock 4 (dongle)",
    (0x054C, 0x0268): "DualShock 3",
    (0x045E, 0x028E): "Xbox 360 Controller",
    (0x045E, 0x02FF): "Xbox One Controller",
    (0x045E, 0x02EA): "Xbox One Controller",
    (0x045E, 0x0B12): "Xbox Series Controller",
    (0x045E, 0x0B13): "Xbox Series Controller",
    (0x045E, 0x0B00): "Xbox Elite 2",
    # Spelled exactly as OUNCE_SELF_NAMES has it: the Ounce targets enumerate
    # as this, and the self-filter compares names.
    (0x057E, 0x2009): "Nintendo Switch Pro Controller",
    (0x057E, 0x2006): "Joy-Con (L)",
    (0x057E, 0x2007): "Joy-Con (R)",
    (0x28DE, 0x1102): "Steam Controller",
    (0x28DE, 0x1142): "Steam Controller (dongle)",
    (0x28DE, 0x11FF): "Steam Input",
    (0x28DE, 0x1205): "Steam Deck",
}

VENDOR_NAMES = {0x054C: "Sony", 0x045E: "Xbox", 0x057E: "Nintendo",
                0x28DE: "Valve", 0x0F0D: "Hori", 0x0E6F: "PDP",
                0x24C6: "PowerA", 0x146B: "Nacon", 0x20D6: "PowerA"}

_under_steam = False       # set by gamepad_available; see pad_name


def pad_usb_ids(index):
    """(vendor, product) for a joystick, from its SDL GUID. (None, None) if
    unavailable - Bluetooth pads and virtual devices do not always carry them."""
    try:
        guid = _pg.joystick.Joystick(index).get_guid()
    except Exception:
        return (None, None)
    if not guid or len(guid) < 20:
        return (None, None)

    def le16(s):
        # SDL packs the GUID little-endian, so each 16-bit field reads back as
        # two byte-swapped hex pairs: "4c05" is 0x054C.
        try:
            return int(s[2:4] + s[0:2], 16) or None
        except ValueError:
            return None

    return (le16(guid[8:12]), le16(guid[16:20]))


def pad_name(index):
    """What to call pad `index` on screen.

    Falls back through: a known vendor/product, the SDL GameController name,
    then the raw joystick name. Whatever comes out, something is always
    returned - a pad with no name is worse than a badly named one."""
    try:
        raw = (_pg.joystick.Joystick(index).get_name() or "").strip()
    except Exception:
        raw = ""
    gc = ""
    try:
        if _sdl_controller.is_controller(index):
            gc = (_sdl_controller.name_forindex(index) or "").strip()
    except Exception:
        pass

    vid, pid = pad_usb_ids(index)
    best = raw or gc
    low = best.lower()
    generic = (not best) or any(low.startswith(g) for g in GENERIC_PAD_NAMES)

    # Valve's own ids first: under Steam Input the pad we are handed is Steam's,
    # whatever is plugged in behind it, and saying so is more use than naming
    # the hardware Steam is hiding.
    if vid == 0x28DE:
        return PAD_NAMES.get((vid, pid), "Steam Input")

    if generic:
        if _under_steam:
            # Inferred, not reported: Steam Input presents its virtual pad as a
            # nameless XInput device indistinguishable from a real 360 pad, so
            # the only evidence that this is Steam's is that Steam launched us.
            # The trailing number is kept so two of them stay tellable apart.
            m = re.search(r"#\s*(\d+)", raw)
            return f"Steam Input #{m.group(1)}" if m else "Steam Input"
        known = PAD_NAMES.get((vid, pid))
        if known:
            return known
        if vid in VENDOR_NAMES:
            return f"{VENDOR_NAMES[vid]} pad"
        return best or f"pad {index}"

    # A named pad still gets normalised when the ids are recognised, so a
    # DualSense is "DualSense" rather than SDL's "PS5 Controller".
    return PAD_NAMES.get((vid, pid), best)


def list_real_pads():
    """Controllers that are not our own emulated targets.

    The Ounce slaves enumerate as Switch Pro Controllers, so they show up in
    the same list as real hardware; offering them as inputs would just feed our
    own output back in."""
    if not gamepad_available():
        return []
    out = []
    for i in range(_pg.joystick.get_count()):
        raw = _pg.joystick.Joystick(i).get_name()
        name = pad_name(i)
        # Both names checked: the display name is what a Switch Pro target
        # normalises to, the raw one is what it reports.
        if raw.strip().lower() in OUNCE_SELF_NAMES or name.lower() in OUNCE_SELF_NAMES:
            continue
        if not _sdl_controller.is_controller(i):
            continue      # no SDL mapping -> its buttons would be arbitrary
        out.append((i, name))
    return out


def prompt_slot_assignments():
    """Ask which input drives each virtual controller.

    Returns {slot: [(kind, ref)]} using the same shape parse_assignment gives,
    or None if the user aborted."""
    pads = list_real_pads()

    print("\n" + "=" * 60)
    print("  Ounce - assign an input to each virtual controller")
    print("=" * 60)
    print("\nDetected inputs:")
    for n, (idx, name) in enumerate(pads, start=1):
        print(f"   {n}) {name}")
    if not pads:
        print("   (no controllers detected - keyboard only)")
    print("   k) Keyboard")
    print("   d) Disabled")
    print("\nThe same input may drive several slots. Press Enter to disable.\n")

    chosen = {}
    for slot in range(NUM_SLOTS):
        while True:
            try:
                raw = input(f"  Controller {slot + 1} (slot {slot}) : ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n[-] Aborted.")
                return None
            if raw in ("", "d", "disable", "disabled", "n", "none"):
                break
            if raw in ("k", "kb", "key", "keyboard"):
                chosen[slot] = [("keyboard", None)]
                break
            if raw.isdigit() and 1 <= int(raw) <= len(pads):
                chosen[slot] = [("pad", str(pads[int(raw) - 1][0]))]
                break
            print("     ? enter a number from the list, 'k', or 'd'")

    if not chosen:
        print("\n[-] Every slot disabled - nothing to drive.")
        return None

    print("\n  Summary:")
    for slot in range(NUM_SLOTS):
        if slot not in chosen:
            print(f"    Controller {slot + 1} (slot {slot}) : disabled")
            continue
        kind, ref = chosen[slot][0]
        if kind == "keyboard":
            label = "Keyboard"
        else:
            label = next((nm for i, nm in pads if str(i) == ref), f"pad {ref}")
        print(f"    Controller {slot + 1} (slot {slot}) : {label}")
    print()
    return chosen


def parse_assignment(spec):
    """Parse one 'SLOT=SOURCE' assignment into ([slots], source).

    SLOT is a slot number, a comma list ('0,2'), or 'all' to drive every slot
    from one source. SOURCE is 'keyboard', or 'pad:N' / 'pad:<name substring>'.
    Assigning more than one source to the same slot merges them, so a slot can
    be driven by a keyboard and a pad at once."""
    if "=" not in spec:
        raise ValueError(f"expected SLOT=SOURCE, got '{spec}'")
    slot_s, src = spec.split("=", 1)
    slot_s = slot_s.strip().lower()

    if slot_s == "all":
        slots = list(range(NUM_SLOTS))
    else:
        slots = []
        for part in slot_s.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                raise ValueError(f"slot must be a number, a comma list, or 'all'; got '{part}'")
            if not 0 <= n < NUM_SLOTS:
                raise ValueError(f"slot {n} out of range 0..{NUM_SLOTS - 1}")
            slots.append(n)
        if not slots:
            raise ValueError("no slots given")
    src = src.strip().lower()
    if src in ("keyboard", "kbd", "key"):
        return slots, ("keyboard", None)
    if src in ("pad", "gamepad", "controller"):
        # No reference given: take the first usable controller. Handy under
        # Steam Input, where you cannot predict what Steam names its virtual
        # pad, and for one-controller setups generally.
        return slots, ("pad", None)
    if src.startswith("pad:") or src.startswith("gamepad:"):
        return slots, ("pad", src.split(":", 1)[1].strip())
    raise ValueError(f"unknown source '{src}' (use keyboard, pad, or pad:N / pad:<name>)")


def resolve_pad(ref):
    """Resolve a pad reference (index or name substring) to a controller index."""
    if not gamepad_available():
        return None
    count = _pg.joystick.get_count()
    try:
        idx = int(ref)
        return idx if 0 <= idx < count else None
    except ValueError:
        pass
    ref_l = ref.lower()
    for i in range(count):
        # Matched against both names, so --assign 1=pad:dualsense works off the
        # name shown in the menu as well as off whatever SDL called it.
        if (ref_l in _pg.joystick.Joystick(i).get_name().lower()
                or ref_l in pad_name(i).lower()):
            return i
    return None


def _merge_axis(a, b):
    """Combine two readings of the same axis: whichever is pushed further from
    centre wins. A resting stick (128) therefore never cancels a key press,
    and a moved stick overrides an untouched keyboard."""
    return a if abs(a - 128) >= abs(b - 128) else b


def merge_inputs(*states):
    """Merge any number of input states. Buttons and Home/Capture are OR'd so
    any source can press anything; each axis takes whichever source is pushed
    furthest from centre, so an idle source never cancels an active one."""
    out = None
    for s in states:
        if s is None:
            continue
        if out is None:
            out = s
            continue
        out = (
            out[0] | s[0],
            _merge_axis(out[1], s[1]),
            _merge_axis(out[2], s[2]),
            _merge_axis(out[3], s[3]),
            _merge_axis(out[4], s[4]),
            out[5] | s[5],
        )
    return out if out is not None else NEUTRAL


# --------------------------------------------------------------------------
# Binding tables. An "action" is a virtual Switch Pro input; a binding maps a
# physical key or pad button onto one. Every slot has its own binding set, so
# each virtual controller can be driven by whatever the user chooses.
# --------------------------------------------------------------------------

BUTTON_ACTIONS = {
    "UP": SPI_MASK_UP, "DOWN": SPI_MASK_DOWN,
    "LEFT": SPI_MASK_LEFT, "RIGHT": SPI_MASK_RIGHT,
    "B1": SPI_MASK_B1, "B2": SPI_MASK_B2, "B3": SPI_MASK_B3, "B4": SPI_MASK_B4,
    "L1": SPI_MASK_L1, "R1": SPI_MASK_R1, "L2": SPI_MASK_L2, "R2": SPI_MASK_R2,
    "S1": SPI_MASK_S1, "S2": SPI_MASK_S2, "L3": SPI_MASK_L3, "R3": SPI_MASK_R3,
}
AUX_ACTIONS = {"HOME": SPI_AUX_MASK_HOME, "CAPTURE": SPI_AUX_MASK_CAPTURE}
# action -> (axis index into [lx, ly, rx, ry], value when pressed)
AXIS_ACTIONS = {
    "LX-": (0, 0), "LX+": (0, 255), "LY-": (1, 0), "LY+": (1, 255),
    "RX-": (2, 0), "RX+": (2, 255), "RY-": (3, 0), "RY+": (3, 255),
}
ALL_ACTIONS = list(BUTTON_ACTIONS) + list(AUX_ACTIONS) + list(AXIS_ACTIONS)

# Every Switch Pro input, in a sensible order for a remapping screen, with the
# names a player recognises rather than the internal B1/S2 codes.
ACTION_DISPLAY = [
    ("LY-", "Left Stick Up"),      ("LY+", "Left Stick Down"),
    ("LX-", "Left Stick Left"),    ("LX+", "Left Stick Right"),
    ("RY-", "Right Stick Up"),     ("RY+", "Right Stick Down"),
    ("RX-", "Right Stick Left"),   ("RX+", "Right Stick Right"),
    ("UP", "D-Pad Up"),            ("DOWN", "D-Pad Down"),
    ("LEFT", "D-Pad Left"),        ("RIGHT", "D-Pad Right"),
    ("B4", "X  (top)"),            ("B3", "Y  (left)"),
    ("B1", "B  (bottom)"),         ("B2", "A  (right)"),
    ("L1", "L"),                   ("R1", "R"),
    ("L2", "ZL"),                  ("R2", "ZR"),
    ("L3", "L3  (left stick click)"),
    ("R3", "R3  (right stick click)"),
    ("S2", "+  (Start)"),          ("S1", "-  (Select)"),
    ("HOME", "Home"),              ("CAPTURE", "Capture"),
]

# Same names, for reporting a single action without scanning the list.
ACTION_LABEL = {a: n for a, n in ACTION_DISPLAY}

# How much of the keyboard drives a given player, on top of any pad it has.
# KB_AUX exists because pads are missing buttons the Switch has - a Steam
# Controller has no Home or Capture - and a player who wants those from the
# keyboard usually does NOT want WASD stealing their stick at the same time.
KB_OFF, KB_AUX, KB_FULL = "off", "aux", "full"
KB_AUX_ACTIONS = ("HOME", "CAPTURE")
KB_MODE_NAMES = {KB_OFF: "off", KB_AUX: "Home/Capture", KB_FULL: "full"}


def pygame_key_to_name(pg, key):
    """pygame key constant -> a name from VK_NAMES, or None if unusable.

    pygame and the Win32 key table disagree on naming, so this normalises
    between them; anything that has no Win32 equivalent is rejected rather than
    silently bound to nothing."""
    raw = pg.key.name(key)
    if not raw:
        return None
    n = raw.strip().upper()
    special = {
        "RETURN": "ENTER", "ESCAPE": "ESC", "PAGE UP": "PAGEUP",
        "PAGE DOWN": "PAGEDOWN", "LEFT SHIFT": "LSHIFT", "RIGHT SHIFT": "RSHIFT",
        "LEFT CTRL": "LCTRL", "RIGHT CTRL": "RCTRL", "LEFT ALT": "ALT",
        "RIGHT ALT": "ALT", "LEFT META": "CTRL", "-": "MINUS", "=": "EQUALS",
        ",": "COMMA", ".": "PERIOD", "/": "SLASH", ";": "SEMICOLON",
        "'": "QUOTE", "[": "LBRACKET", "]": "RBRACKET", "\\": "BACKSLASH",
        "`": "GRAVE",
    }
    n = special.get(n, n)
    if n.startswith("[") and n.endswith("]"):      # numpad, e.g. "[7]"
        n = "NUM" + n[1:-1]
    return n if n in VK_NAMES else None


def _build_vk_names():
    t = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        t[c] = ord(c)
    t.update({
        "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
        "SPACE": 0x20, "ENTER": 0x0D, "TAB": 0x09, "ESC": 0x1B,
        "BACKSPACE": 0x08, "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12,
        "LSHIFT": 0xA0, "RSHIFT": 0xA1, "LCTRL": 0xA2, "RCTRL": 0xA3,
        "COMMA": 0xBC, "PERIOD": 0xBE, "SLASH": 0xBF, "SEMICOLON": 0xBA,
        "QUOTE": 0xDE, "LBRACKET": 0xDB, "RBRACKET": 0xDD, "BACKSLASH": 0xDC,
        "MINUS": 0xBD, "EQUALS": 0xBB, "GRAVE": 0xC0,
        "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
        "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    })
    for i in range(1, 13):
        t[f"F{i}"] = 0x6F + i
    for i in range(10):
        t[f"NUM{i}"] = 0x60 + i
    return t


VK_NAMES = _build_vk_names()

# Default pad bindings, by SDL GameController button name. Face buttons are
# positional, so a DualSense cross (bottom) lands on the Switch's B (bottom).
DEFAULT_PAD_BUTTONS = {
    "UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT", "RIGHT": "DPAD_RIGHT",
    "B1": "A", "B2": "B", "B3": "X", "B4": "Y",
    "L1": "LEFTSHOULDER", "R1": "RIGHTSHOULDER",
    "S1": "BACK", "S2": "START", "L3": "LEFTSTICK", "R3": "RIGHTSTICK",
    "HOME": "GUIDE", "CAPTURE": "MISC1",
}

DEFAULT_KEYBOARD_BINDINGS = {
    "LX-": "A", "LX+": "D", "LY-": "W", "LY+": "S",
    "RX-": "7", "RX+": "9", "RY-": "8", "RY+": "0",
    "UP": "UP", "DOWN": "DOWN", "LEFT": "LEFT", "RIGHT": "RIGHT",
    "B4": "I", "B3": "J", "B1": "K", "B2": "L",
    "L2": "Y", "L1": "U", "R1": "O", "R2": "P",
    "S2": "N", "S1": "M", "L3": "Z", "R3": "X",
    "HOME": "H", "CAPTURE": "C",
}


def validate_bindings(bindings, where, pad=False):
    """Reject unknown actions/keys loudly rather than silently ignoring them -
    a typo in a config file should not present as a dead button."""
    errs = []
    for action, phys in bindings.items():
        if action not in ALL_ACTIONS:
            errs.append(f"{where}: unknown action '{action}'")
        elif not pad and str(phys).upper() not in VK_NAMES:
            errs.append(f"{where}: unknown key '{phys}' for action '{action}'")
    return errs


def read_keyboard_mapped(bindings):
    """Read a keyboard binding set into (buttons, lx, ly, rx, ry, aux)."""
    buttons = 0
    aux = 0
    axes = [128, 128, 128, 128]
    for action, key in bindings.items():
        vk = VK_NAMES.get(str(key).upper())
        if vk is None or not is_key_down(vk):
            continue
        if action in BUTTON_ACTIONS:
            buttons |= BUTTON_ACTIONS[action]
        elif action in AUX_ACTIONS:
            aux |= AUX_ACTIONS[action]
        elif action in AXIS_ACTIONS:
            idx, val = AXIS_ACTIONS[action]
            axes[idx] = val
    return (buttons, axes[0], axes[1], axes[2], axes[3], aux)



# --------------------------------------------------------------------------
# Physical controller input (DualSense / Steam Controller / XInput / etc.)
#
# These three speak completely different protocols natively - the DualSense is
# raw HID, the Steam Controller goes through Steam's own driver, and XInput is
# a Windows API - so rather than three code paths we let SDL2 (via pygame)
# normalise them all to its GameController layout. Anything SDL recognises as
# a game controller works, including pads not listed above.
# --------------------------------------------------------------------------

# The Ounce target itself enumerates as a Switch Pro Controller. Reading it
# back in would form a feedback loop (we would be echoing our own output), so
# it is skipped during auto-detection unless explicitly selected by index.
OUNCE_SELF_NAMES = ("nintendo switch pro controller",)

_pg = None            # pygame module, imported lazily
_sdl_controller = None
_window = None        # status window, only under Steam (see gamepad_available)


# --------------------------------------------------------------------------
# Capture card preview (video + audio) via ffmpeg.
#
# The window has to exist anyway for Steam Input to attach, so showing the
# capture card in it means the window you must keep focused is also the one
# you want to look at.
# --------------------------------------------------------------------------

# Preview window size. With the VLC backend this is the actual display
# resolution - VLC renders into this window, so a small window means a small
# picture no matter how good the source is. The ffmpeg fallback scales to it.
WINDOW_W, WINDOW_H = 1280, 720
CAPTURE_W, CAPTURE_H = 960, 540      # ffmpeg-path pipe size only
_NO_WINDOW = 0x08000000        # subprocess CREATE_NO_WINDOW, so ffmpeg stays hidden

# How many milliseconds the VLC capture path is allowed to buffer.
#
# This was 0, on the reasoning that any buffer is latency you can feel. It is -
# but zero is past the point where the audio output can absorb a scheduling
# hiccup, and a starved audio output does not arrive late, it crackles and drops
# out. It went to 100 for that reason, 50 having been measured here as
# almost-but-not-quite enough.
#
# 20 now, because with SPLIT_AUDIO the sound no longer comes through VLC at all
# and this buffers only the picture. That matters: played off this window rather
# than the card's HDMI passthrough, the buffer is what you feel - the whole input
# chain is ~1.7ms against the 100ms this used to add. Whatever lag is left below
# this figure is the card's own pipeline, which no setting here can shorten.
#
# Still tunable with --capture-latency, because the floor depends on the machine
# and only running it on the hardware shows where that floor is.
CAPTURE_LATENCY_MS = 20


def set_capture_latency(ms):
    global CAPTURE_LATENCY_MS
    CAPTURE_LATENCY_MS = max(0, int(ms))


# How much audio the separate pipe queues ahead, in milliseconds.
#
# Only used when audio is split out of VLC. It buys back, for the sound alone,
# the slack that --capture-latency used to give both streams together: audio
# runs this far behind the picture, and the picture is not delayed at all.
#
# 100 because that is the figure already measured as comfortable on this machine
# back when VLC was doing the buffering. Audio arriving after video is the
# forgiving direction - broadcast practice tolerates about 125ms of it, against
# roughly 45ms the other way - so there is room to be generous here in a way
# there is not with --capture-latency.
AUDIO_LATENCY_MS = 100


def set_audio_latency(ms):
    global AUDIO_LATENCY_MS
    AUDIO_LATENCY_MS = max(0, int(ms))


# What the toolbar offers for each. Both are tuning-by-ear settings - the right
# value depends on the machine and there is no way to work it out from here - so
# they are on the menu rather than only on the command line, where changing one
# means quitting mid-session and remembering a flag.
VIDEO_LATENCY_CHOICES = (0, 10, 20, 40, 60, 100, 150)
AUDIO_LATENCY_CHOICES = (40, 60, 80, 100, 150, 200, 300)


# --------------------------------------------------------------------------
# Internal upscaler.
#
# The card sends 1440p, but the game renders well below that and the console
# scales it up, so the stairsteps are baked into the signal before it ever
# arrives. Nothing downstream can put back detail that was never drawn.
#
# What does help is the thing you notice by accident: shrinking the window
# makes the picture look good. A downscale averages each stairstep away, and
# that is supersampling - the strongest antialiasing there is. This reproduces
# it at any window size. Resample down to UPSCALE_RENDER_H, then back up to the
# window, so the picture keeps the shrunken-window look while still filling the
# screen. Lower render height is stronger AA and a softer image; the right
# value is the one that looks right on the game being played, which is why it
# is on the toolbar rather than only in a flag.
#
# Both resamples run in linear light. Averaging gamma-encoded pixels averages
# the wrong numbers and darkens every edge, which is exactly what makes a naive
# downscale look muddy rather than smooth. The card sends untagged frames, so
# the chain must declare what they are before it can linearise them - that is
# what setparams is for, and leaving it out does not degrade quietly, it fails
# outright with "no path between colorspaces".
#
# ffmpeg backend only. The VLC backend hands frames straight to the GPU and
# never lets Python, or a filter chain, near them.
UPSCALE = "off"             # off | fast | aa
UPSCALE_RENDER_H = 720      # the shrink; 0 disables it
UPSCALE_SHARPEN = 0.4       # CAS strength afterwards; 0.0 = none

UPSCALE_CHOICES = ("off", "fast", "aa")
# 0 means "no shrink": resample straight to the window, in linear light.
RENDER_H_CHOICES = (480, 540, 720, 900, 1080, 0)
SHARPEN_CHOICES = (0.0, 0.2, 0.4, 0.6, 0.8)


def set_upscale(mode):
    global UPSCALE
    UPSCALE = mode if mode in UPSCALE_CHOICES else "off"


def set_render_height(h):
    global UPSCALE_RENDER_H
    UPSCALE_RENDER_H = max(0, int(h))


def set_sharpen(s):
    global UPSCALE_SHARPEN
    UPSCALE_SHARPEN = max(0.0, min(1.0, float(s)))


def _fit(sw, sh, mw, mh):
    """Largest box with sw:sh proportions fitting inside mw x mh.

    Even sizes only - odd dimensions break chroma-subsampled intermediate
    formats, and the failure shows up as a filter graph that will not build."""
    s = min(mw / sw, mh / sh)
    return max(2, int(sw * s) // 2 * 2), max(2, int(sh * s) // 2 * 2)


def colour_tag(src_size):
    """Say which YUV matrix the card's untagged frames use.

    Nothing in a raw DirectShow stream states this, so every consumer guesses,
    and swscale guesses bt601 whatever the resolution. On a 1440p feed that is
    simply wrong - HD is bt709 - and it is measurable: the two matrices differ
    by about 4 code values on a colour bar, enough that turning the upscaler on
    would visibly shift the picture if only one path were tagged. So the tag
    goes on every chain, and it follows the standard rule rather than being
    pinned: bt709 at HD and above, bt601 below it, which is right for the
    640x480 mode DirectShow falls back to."""
    if not src_size or not src_size[1]:
        return None            # nothing to base it on; leave the guess alone
    space = "bt709" if src_size[1] >= 720 else "smpte170m"
    return (f"setparams=color_primaries={space}:color_trc={space}"
            f":colorspace={space}:range=tv")


def upscale_chain(out_w, out_h, src_size=None):
    """The -vf chain for the preview pipe.

    Always emits exactly out_w x out_h, padded if the source is a different
    shape: the reader reads fixed-size frames off the pipe and reshapes them,
    so a chain that emitted anything else would desynchronise the stream."""
    pad = f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2"
    fit = f"force_original_aspect_ratio=decrease"
    tag = colour_tag(src_size)
    head = f"{tag}," if tag else ""
    if UPSCALE == "off":
        return f"{head}scale={out_w}:{out_h}:{fit},{pad}"
    if UPSCALE == "fast" or not src_size or not src_size[1]:
        # Either asked for cheap, or there is no source geometry to compute a
        # shrink from. A single good-kernel resample straight to the window is
        # still strictly better than the old fixed 960x540 followed by a
        # pygame stretch back up - that was two lossy resamples, neither of
        # them at the size actually being displayed.
        return f"{head}scale={out_w}:{out_h}:{fit}:flags=lanczos+accurate_rnd,{pad}"

    sw, sh = src_size
    fw, fh = _fit(sw, sh, out_w, out_h)
    steps = [tag, "zscale=t=linear", "format=gbrpf32le"]
    # Only shrink if it really is a shrink. Asking for a render height above
    # what the window shows would upscale and then downscale, which costs
    # sharpness to gain nothing.
    if UPSCALE_RENDER_H and UPSCALE_RENDER_H < fh:
        steps.append("zscale=%d:%d:f=spline36" % _fit(sw, sh, out_w, UPSCALE_RENDER_H))
    # Back to the display transfer while still in float, and only then down to
    # 8 bits. The other order quantises linear light, which has nowhere near
    # enough codes for the shadows: it cost 13 code values on a dark patch
    # here, measured against the unfiltered path, and showed up as the AA modes
    # looking murkier rather than smoother.
    steps += [f"zscale={fw}:{fh}:f=spline36",
              "zscale=t=" + ("bt709" if sh >= 720 else "smpte170m"),
              "format=gbrp"]
    if UPSCALE_SHARPEN > 0:
        steps.append(f"cas=strength={UPSCALE_SHARPEN:.2f}")
    steps.append(pad)
    return ",".join(steps)


def ffmpeg_exe():
    """Path to an ffmpeg binary: PATH first, else the pip-installed one."""
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def list_dshow_devices():
    """(video_names, audio_names) that ffmpeg can capture from."""
    exe = ffmpeg_exe()
    if not exe:
        return [], []
    try:
        r = subprocess.run([exe, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, timeout=25,
                           creationflags=_NO_WINDOW)
    except Exception:
        return [], []
    video, audio = [], []
    for line in (r.stderr or "").splitlines():
        m = re.search(r'"([^"]+)"\s*\((video|audio)\)', line)
        if m:
            (video if m.group(2) == "video" else audio).append(m.group(1))
    return video, audio


def list_dshow_modes(device_name):
    """Video modes a capture device advertises: [(w, h, fps, pixfmt), ...].

    This matters more than it looks: DirectShow hands out the FIRST advertised
    format unless you ask for something specific, and on capture cards that is
    usually the smallest (640x480). Not specifying a mode does not mean
    'native' - it means 'lowest'."""
    exe = ffmpeg_exe()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-hide_banner", "-f", "dshow",
                            "-list_options", "true", "-i", f"video={device_name}"],
                           capture_output=True, text=True, timeout=60,
                           creationflags=_NO_WINDOW)
    except Exception:
        return []
    modes, seen = [], set()
    for line in (r.stderr or "").splitlines():
        # Both raw AND compressed formats matter. Raw (pixel_format=) tops out
        # at 4K30 on this class of card because uncompressed 4K60 will not fit
        # the USB link; the high modes - 4K60, 1440p144, 1080p240 - are only
        # offered as vcodec=mjpeg. Parsing just pixel_format= silently caps you
        # at half the frame rate the card can actually deliver.
        m = re.search(r"(?:vcodec=(\w+)|pixel_format=(\w+)).*?"
                      r"max s=(\d+)x(\d+) fps=([\d.]+)", line)
        if not m:
            continue
        fmt = m.group(1) or m.group(2)
        w, h, fps = int(m.group(3)), int(m.group(4)), round(float(m.group(5)))
        key = (w, h, fps, fmt)
        if key not in seen:
            seen.add(key)
            modes.append((w, h, fps, fmt))
    return modes


# Preferred default: 1440p raw. Raw skips the card's MJPEG compression, so the
# picture is not twice-compressed before it reaches the screen, and 1440p60 raw
# is a rate the USB link and the decoder both keep up with - measured 41fps at
# 4K60 MJPEG on this machine, so "4K60" was never really 60 anyway.
DEFAULT_MODE = (2560, 1440)
RAW_FORMATS = ("nv12", "yuv420p", "yuyv422")


def pick_best_mode(modes, want=None):
    """Choose a capture mode. `want` is 'WxH', 'WxH@FPS', or None for the default.

    With no preference: 1440p raw if the card offers it, else highest
    resolution first and then the highest frame rate at that resolution.
    Resolution is ranked ahead of frame rate in that fallback deliberately -
    maximising pixels-per-second instead would pick 1440p144 over 4K60, which
    is not what someone with a 4K source expects to see."""
    if not modes:
        return None
    if want:
        m = re.match(r"(\d+)x(\d+)(?:@([\d.]+))?$", want.strip().lower())
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            fps = round(float(m.group(3))) if m.group(3) else None
            cands = [x for x in modes if x[0] == w and x[1] == h
                     and (fps is None or x[2] == fps)]
            if cands:
                return max(cands, key=lambda x: x[2])
    raw1440 = [x for x in modes if (x[0], x[1]) == DEFAULT_MODE
               and x[3].lower() in RAW_FORMATS]
    if raw1440:
        # nv12 ahead of the others at equal frame rate: it is half the bytes of
        # yuyv422 and the format the card hands over natively.
        return max(raw1440, key=lambda x: (x[2], x[3].lower() == "nv12"))
    return max(modes, key=lambda x: (x[0] * x[1], x[2]))


def pick_capture(ref, names, keywords=("elgato", "capture", "hdmi")):
    """Resolve a device reference (index or name substring) against `names`."""
    if not names:
        return None
    if ref is None:
        for n in names:                       # prefer a capture card
            if any(k in n.lower() for k in keywords):
                return n
        return names[0]
    try:
        i = int(ref)
        return names[i] if 0 <= i < len(names) else None
    except ValueError:
        pass
    for n in names:
        if ref.lower() in n.lower():
            return n
    return None


RAW_FORMATS = ("nv12", "yuv420p", "yuyv422", "rgb24", "bgr24", "uyvy422")


def mode_label(m):
    w, h, fps, fmt = m
    return f"{w}x{h} @{fps} {fmt}"


def group_modes(modes):
    """Split modes into (raw, compressed), each sorted best-first.

    The distinction is not cosmetic: raw formats are uncompressed and so are
    bandwidth-limited over USB (this card caps at 4K30), while compressed ones
    (mjpeg) carry 4K60, 1440p144 and 1080p240. Someone picking a mode needs to
    see which side of that line they are on."""
    def dedupe(ms, prefer):
        """One entry per resolution+rate.

        The card advertises the same resolution under several pixel formats
        (nv12, yuv420p, yuyv422...), which triples the list for no user-visible
        benefit and pushes the lower resolutions off the bottom of the menu.
        Keep one per (w, h, fps), preferring the format that decodes best."""
        best = {}
        for m in ms:
            k = (m[0], m[1], m[2])
            cur = best.get(k)
            if cur is None or (m[3].lower() in prefer and cur[3].lower() not in prefer):
                best[k] = m
        return list(best.values())

    raw = dedupe([m for m in modes if m[3].lower() in RAW_FORMATS],
                 prefer=("nv12", "yuv420p"))
    comp = dedupe([m for m in modes if m[3].lower() not in RAW_FORMATS],
                  prefer=("mjpeg",))
    key = lambda m: (m[0] * m[1], m[2])
    return sorted(raw, key=key, reverse=True), sorted(comp, key=key, reverse=True)


class Toolbar:
    """Top strip: pick the capture device and the capture mode.

    Deliberately drawn by pygame rather than being native controls - it lives
    in the same window as the video and has to appear and disappear with
    fullscreen, which is far simpler to do by just not drawing it."""

    def __init__(self):
        self.devices = []
        self.modes_raw = []
        self.modes_comp = []
        self.device = None
        self.mode = None
        self.pads = []               # [(idx, name)] real controllers
        self.slots = {}              # slot -> pad index, or None for no pad
        self.slot_kb = {}            # slot -> KB_FULL | KB_AUX | KB_OFF
        self.split_audio = False     # audio on its own pipe -> its own buffer row
        # The upscaler is a filter chain in the preview pipe, so it exists only
        # on the ffmpeg backend. On the VLC backend frames go straight to the
        # GPU and never pass through anything we could filter, so the rows are
        # hidden rather than shown doing nothing.
        self.software_preview = False
        # Shaders are an mpv thing: VLC has no programmable stage at all, and
        # the ffmpeg path filters on the CPU instead. Rows gate on this.
        self.mpv_preview = False
        self.shader = None            # currently loaded shader path, or None
        self.scaler = "ewa_lanczossharp"
        self.dscale = "mitchell"
        self.supersampling = None     # (active, why) from the player, or None
        self.open_menu = None        # None|'device'|'mode'|'vlat'|'alat'|'slot0'..|'keys'
        self._hit = []               # [(rect, kind, value)] rebuilt each draw
        self.keymap = None           # live dict of action -> key name
        self.capture_action = None   # action awaiting a keypress, if any
        self.recording = False       # drives the record button's appearance

    def dscale_label(self):
        """The supersample row, which reports whether it is doing anything.

        Selecting a kernel is not the same as that kernel having work to do:
        supersampling only happens while the window is smaller than the source,
        and at 1:1 the setting is real but idle. A row showing only the kernel
        name would imply otherwise, so the state goes in the label."""
        active, why = (self.supersampling or (None, None))
        if active is None:
            return f"Supersample :  {self.dscale}"
        return "Supersample :  %s   (%s)" % (
            self.dscale, f"active, {why}" if active else f"idle: {why}")

    def set_sources(self, devices, modes, device, mode):
        self.devices = devices
        self.modes_raw, self.modes_comp = group_modes(modes)
        self.device = device
        self.mode = mode

    def set_inputs(self, pads, slots, slot_kb=None):
        self.pads = pads
        self.slots = dict(slots)
        # slot -> True when the keyboard drives this player *alongside* a pad.
        # Kept separate from self.slots, which holds the primary source: the
        # two are not alternatives, they are merged.
        self.slot_kb = dict(slot_kb or {})

    def slot_active(self, slot):
        """A player is driven if it has a pad, any keyboard, or both."""
        return (self.slots.get(slot) is not None
                or self.slot_kb.get(slot, KB_OFF) != KB_OFF)

    def _slot_label(self, slot, short=True):
        pad = self.slots.get(slot)
        kb = self.slot_kb.get(slot, KB_OFF)
        if pad is None:
            if kb == KB_FULL:
                return "kbd" if short else "Keyboard"
            if kb == KB_AUX:
                return "H/C" if short else "Keyboard (Home/Capture only)"
            return "off" if short else "Disabled"
        name = next((n for i, n in self.pads if i == pad), f"pad{pad}")
        # Only the short form drops a parenthesised suffix. In the menu it is
        # kept, because that is where "Joy-Con (L)" has to stay tellable from
        # "Joy-Con (R)".
        name = (name.split("(")[0].strip()[:14] if short else name[:40])
        if kb == KB_FULL:
            return name + (" + kbd" if short else " + keyboard")
        if kb == KB_AUX:
            return name + (" + H/C" if short else " + keyboard Home/Capture")
        return name

    def _font(self, size):
        return _pg.font.SysFont("segoeui", size) or _pg.font.SysFont("consolas", size)

    def draw(self, surface):
        """Collapsed strip with a single Settings button; everything else lives
        in a panel that drops down from it.

        Kept collapsed by default so the strip stealing height from the video
        stays as small as possible - a permanent row of seven buttons was both
        cramped and always in the way."""
        self._hit = []
        w = surface.get_width()
        f = self._font(13)

        _pg.draw.rect(surface, (28, 28, 34), (0, 0, w, TOOLBAR_H))
        _pg.draw.line(surface, (60, 60, 72), (0, TOOLBAR_H - 1), (w, TOOLBAR_H - 1))

        cap = "Settings " + ("^" if self.open_menu else "v")
        txt = f.render(cap, True, (230, 230, 240))
        rect = _pg.Rect(6, 3, txt.get_width() + 16, TOOLBAR_H - 6)
        _pg.draw.rect(surface, (52, 52, 64) if self.open_menu else (40, 40, 50),
                      rect, border_radius=4)
        surface.blit(txt, (rect.x + 8, rect.y + 3))
        self._hit.append((rect, "open", "root"))

        # Record sits on the strip rather than in the dropdown: it is the one
        # control you need to hit immediately and read the state of at a
        # glance, which a collapsed menu cannot give you.
        rcap = "Stop" if self.recording else "Rec"
        rtxt = f.render(rcap, True, (255, 235, 235) if self.recording
                        else (230, 230, 240))
        rrect = _pg.Rect(rect.right + 6, 3, rtxt.get_width() + 30, TOOLBAR_H - 6)
        _pg.draw.rect(surface, (150, 40, 44) if self.recording else (40, 40, 50),
                      rrect, border_radius=4)
        _pg.draw.circle(surface, (235, 70, 70), (rrect.x + 12, rrect.centery), 5)
        surface.blit(rtxt, (rrect.x + 22, rrect.y + 3))
        self._hit.append((rrect, "record", not self.recording))
        rect = rrect                 # what the summary is positioned after

        # Live summary next to the button, so the common case needs no clicks.
        on = [f"P{s + 1}" for s in range(NUM_SLOTS) if self.slot_active(s)]
        bits = [mode_label(self.mode) if self.mode else "no mode",
                ("+".join(on) if on else "no controllers")]
        summary = f.render("   " + "   |   ".join(bits), True, (150, 150, 168))
        surface.blit(summary, (rect.right + 4, rect.y + 3))

        hint = f.render("F11 fullscreen", True, (110, 110, 128))
        if rect.right + summary.get_width() < w - hint.get_width() - 14:
            surface.blit(hint, (w - hint.get_width() - 8, rect.y + 3))

        if self.open_menu:
            self._draw_menu(surface, f)

    def _draw_menu(self, surface, f):
        items = []
        menu_x = 6
        if self.open_menu == "root":
            # Top level of the dropdown: every setting with its current value,
            # each opening its own list.
            items = [(None, "-- capture --", None),
                     ("go", f"Input :  {self.device or 'none'}", "device"),
                     ("go", f"Mode  :  {mode_label(self.mode) if self.mode else 'default'}",
                      "mode"),
                     (None, "-- latency --", None)]
            # live-caching is a VLC knob. Neither the ffmpeg pipe nor mpv has
            # one, so the row would be inert on both.
            if not self.software_preview and not self.mpv_preview:
                items.append(("go", f"Video buffer :  {CAPTURE_LATENCY_MS} ms",
                              "vlat"))
            # Audio buffering is only a thing on its own pipe; with audio inside
            # VLC the video buffer is the audio buffer and a second row would be
            # offering a setting that does nothing.
            if self.split_audio:
                items.append(("go", f"Audio buffer :  {AUDIO_LATENCY_MS} ms", "alat"))
            if self.mpv_preview:
                items.append((None, "-- picture --", None))
                items.append(("go", "Shader :  %s" %
                              (os.path.basename(self.shader)[:-5] if self.shader
                               else "none"), "shader"))
                items.append(("go", f"Scaler :  {self.scaler}", "scaler"))
                items.append(("go", self.dscale_label(), "dscale"))
            if self.software_preview:
                items.append((None, "-- picture --", None))
                items.append(("go", f"Upscaler :  {UPSCALE}", "up"))
                # Only meaningful once there is a resample to tune.
                if UPSCALE != "off":
                    items.append(("go", "Render   :  %s" %
                                  (f"{UPSCALE_RENDER_H}p" if UPSCALE_RENDER_H
                                   else "native"), "rh"))
                    items.append(("go", f"Sharpen  :  {UPSCALE_SHARPEN:.1f}", "sharp"))
            items.append((None, "-- controllers --", None))
            # Spell out what each controller is currently driven by, rather
            # than an abbreviation - this is the screen people come here to read.
            items += [("go", f"Controller {s + 1} :  {self._slot_label(s, short=False)}",
                       f"slot{s}") for s in range(NUM_SLOTS)]
            items += [(None, "-- keyboard --", None),
                      ("go", "Remap keyboard controls...", "keys")]
        elif self.open_menu == "device":
            items = [("device", d, d) for d in self.devices]
        elif self.open_menu == "vlat":
            items = [(None, "-- video buffer: lower is less delay --", None)]
            items += [("vlat", f"{v} ms" + ("   (default)" if v == 20 else ""), v)
                      for v in VIDEO_LATENCY_CHOICES]
            items.append((None, "-- what is left below this is the card --", None))
            items.append(("go", "< back", "root"))
        elif self.open_menu == "alat":
            items = [(None, "-- audio buffer: raise if sound crackles --", None)]
            items += [("alat", f"{v} ms" + ("   (default)" if v == 100 else ""), v)
                      for v in AUDIO_LATENCY_CHOICES]
            items.append((None, "-- delays sound only, never the picture --", None))
            items.append(("go", "< back", "root"))
        elif self.open_menu == "shader":
            items = [(None, "-- shader: runs on the GPU between decode --", None),
                     ("shader", "none", None)]
            # Whatever is in the user's mpv shaders folder. Nothing is shipped:
            # people who use mpv already curate this, and Anime4K in particular
            # is a large collection with its own licence.
            found = list_shaders()
            if not found:
                items.append((None, "-- none found in %APPDATA%/mpv/shaders --", None))
            items += [("shader", label, path) for label, path in found]
            items.append(("go", "< back", "root"))
        elif self.open_menu == "dscale":
            items = [(None, "-- supersampling: kernel that shrinks the picture --",
                      None)]
            items += [("dscale", s + ("   (default)" if s == "mitchell" else
                                      "   (mpv default, sharper)" if s == "hermite"
                                      else ""), s)
                      for s in DSCALE_CHOICES]
            active, why = (self.supersampling or (None, None))
            if active is not None:
                items.append((None, "-- %s --" % why, None))
                if not active:
                    items.append((None, "-- shrink the window to make it work --",
                                  None))
            items.append(("go", "< back", "root"))
        elif self.open_menu == "scaler":
            items = [(None, "-- scaler: only acts when window != source --", None)]
            items += [("scaler", s + ("   (default)"
                                      if s == "ewa_lanczossharp" else ""), s)
                      for s in SCALER_CHOICES]
            items.append(("go", "< back", "root"))
        elif self.open_menu == "up":
            items = [(None, "-- upscaler --", None),
                     ("up", "off      no filtering, cheapest", "off"),
                     ("up", "fast     one good resample to the window", "fast"),
                     ("up", "aa       shrink and rebuild (strongest AA)", "aa")]
            items.append((None, "-- aa is the shrunken-window look, full size --", None))
            items.append(("go", "< back", "root"))
        elif self.open_menu == "rh":
            items = [(None, "-- render height: lower = smoother, softer --", None)]
            items += [("rh", ("native (no shrink)" if v == 0 else f"{v}p")
                       + ("   (default)" if v == 720 else ""), v)
                      for v in RENDER_H_CHOICES]
            items.append((None, "-- the game renders below this anyway --", None))
            items.append(("go", "< back", "root"))
        elif self.open_menu == "sharp":
            items = [(None, "-- sharpen: puts back what the resample costs --", None)]
            items += [("sharp", ("off" if v == 0 else f"{v:.1f}")
                       + ("   (default)" if v == 0.4 else ""), v)
                      for v in SHARPEN_CHOICES]
            items.append(("go", "< back", "root"))
        elif self.open_menu == "keys":
            self._draw_keymap(surface, f)
            return
        elif self.open_menu and self.open_menu.startswith("slot"):
            slot = int(self.open_menu[4:])
            # Controller first, keyboard second: the pad is the main thing a
            # player is on, and how much keyboard rides along is a separate
            # question answered after it.
            pad, kb = self.slots.get(slot), self.slot_kb.get(slot, KB_OFF)
            items = [(None, f"-- Controller {slot + 1}  (now: "
                            f"{self._slot_label(slot, short=False)}) --", None),
                     (None, "-- controller --", None)]
            items += [("slot", f"{'* ' if idx == pad else '  '}{name}",
                       (slot, idx)) for idx, name in self.pads]
            items.append(("slot", f"{'* ' if pad is None else '  '}No controller",
                          (slot, None)))
            items.append((None, "-- keyboard --", None))
            for mode, label in ((KB_FULL, "Full keyboard"),
                                (KB_AUX, "Home / Capture only"),
                                (KB_OFF, "No keyboard")):
                items.append(("slotkb", f"{'* ' if kb == mode else '  '}{label}",
                              (slot, mode)))
            items.append(("go", "< back", "root"))
        else:
            # Grouped, because raw vs compressed is what decides whether 4K60
            # is even on the table.
            if self.modes_comp:
                items.append((None, "-- compressed (highest modes) --", None))
                items += [("mode", mode_label(m), m) for m in self.modes_comp]
            if self.modes_raw:
                items.append((None, "-- raw (uncompressed) --", None))
                items += [("mode", mode_label(m), m) for m in self.modes_raw]

        rowh, pad = 22, 6
        colw = max([f.size(t)[0] for _, t, _ in items] or [200]) + 28
        mx, my = menu_x, TOOLBAR_H

        # Wrap into columns rather than running off the bottom. This list used
        # to be truncated silently at the window edge, which with a real mpv
        # shader folder - 39 files here - hid a third of it with nothing to say
        # so. Columns are capped so a long list narrows the rows instead of
        # marching off the right edge.
        avail_h = surface.get_height() - my - 10
        per_col = max(1, (avail_h - pad * 2) // rowh)
        ncols = max(1, min(4, -(-len(items) // per_col)))
        if ncols > 1:
            colw = min(colw, (surface.get_width() - 16) // ncols)
            per_col = -(-len(items) // ncols)      # even them out
        wmenu = colw * ncols
        hmenu = min(rowh * min(len(items), per_col) + pad * 2, avail_h)
        mx = max(4, min(mx, surface.get_width() - wmenu - 4))

        _pg.draw.rect(surface, (24, 24, 30), (mx, my, wmenu, hmenu))
        _pg.draw.rect(surface, (70, 70, 84), (mx, my, wmenu, hmenu), 1)

        col, row = 0, 0
        for kind, text, value in items:
            if row >= per_col:
                col, row = col + 1, 0
                if col >= ncols:
                    break
            cx = mx + col * colw           # left edge of the column being drawn
            y = my + pad + row * rowh
            row += 1
            if kind is None:
                surface.blit(f.render(text, True, (130, 130, 150)), (cx + 8, y + 3))
            else:
                if kind == "device":
                    sel = value == self.device
                elif kind == "mode":
                    sel = value == self.mode
                elif kind == "vlat":
                    sel = value == CAPTURE_LATENCY_MS
                elif kind == "alat":
                    sel = value == AUDIO_LATENCY_MS
                elif kind == "shader":
                    sel = value == self.shader
                elif kind == "scaler":
                    sel = value == self.scaler
                elif kind == "dscale":
                    sel = value == self.dscale
                elif kind == "up":
                    sel = value == UPSCALE
                elif kind == "rh":
                    sel = value == UPSCALE_RENDER_H
                elif kind == "sharp":
                    sel = abs(value - UPSCALE_SHARPEN) < 0.001
                elif kind == "slot":
                    sel = self.slots.get(value[0]) == value[1]
                else:
                    sel = False          # 'go' rows are navigation, never selected
                rect = _pg.Rect(cx + 2, y, colw - 4, rowh)
                if sel:
                    _pg.draw.rect(surface, (48, 74, 58), rect, border_radius=3)
                surface.blit(f.render(text, True, (235, 235, 245)), (cx + 8, y + 3))
                self._hit.append((rect, kind, value))

    def _draw_keymap(self, surface, f):
        """Two-column list of every Switch Pro input and the key bound to it.

        Two columns because there are 26 inputs - a single column would run off
        the bottom of the window and the lower half would be unreachable."""
        km = self.keymap or {}
        W, H = surface.get_width(), surface.get_height()
        x0, y0 = 6, TOOLBAR_H
        _pg.draw.rect(surface, (24, 24, 30), (x0, y0, W - 12, H - y0 - 6))
        _pg.draw.rect(surface, (70, 70, 84), (x0, y0, W - 12, H - y0 - 6), 1)

        head = ("Click an input, then press a key.  Esc cancels."
                if not self.capture_action else
                f"Press a key for  {dict(ACTION_DISPLAY).get(self.capture_action, self.capture_action)}"
                "   (Esc to cancel)")
        surface.blit(f.render(head, True,
                              (255, 210, 120) if self.capture_action else (150, 150, 168)),
                     (x0 + 10, y0 + 6))

        rowh = 20
        top = y0 + 28
        per_col = max(1, (H - top - 34) // rowh)
        colw = (W - 24) // 2

        for i, (action, label) in enumerate(ACTION_DISPLAY):
            col, row = divmod(i, per_col)
            if col > 1:
                break
            rx = x0 + 6 + col * colw
            ry = top + row * rowh
            rect = _pg.Rect(rx, ry, colw - 8, rowh - 2)
            capturing = self.capture_action == action
            if capturing:
                _pg.draw.rect(surface, (90, 70, 30), rect, border_radius=3)
            surface.blit(f.render(label, True, (225, 225, 235)), (rx + 6, ry + 2))
            key = km.get(action, "--")
            kt = f.render(str(key), True, (140, 220, 160) if key != "--" else (110, 110, 128))
            surface.blit(kt, (rx + colw - 16 - kt.get_width(), ry + 2))
            self._hit.append((rect, "bindkey", action))

        back = _pg.Rect(x0 + 6, H - 30, 90, 22)
        _pg.draw.rect(surface, (40, 40, 50), back, border_radius=4)
        surface.blit(f.render("< back", True, (225, 225, 235)), (back.x + 10, back.y + 3))
        self._hit.append((back, "go", "root"))

        rst = _pg.Rect(x0 + 104, H - 30, 110, 22)
        _pg.draw.rect(surface, (40, 40, 50), rst, border_radius=4)
        surface.blit(f.render("reset all", True, (225, 225, 235)), (rst.x + 10, rst.y + 3))
        self._hit.append((rst, "keyreset", None))

    def bind_key(self, pg, key):
        """Bind the pending input to `key`. Returns True if the map changed."""
        if not self.capture_action or self.keymap is None:
            return False
        if key == pg.K_ESCAPE:
            self.capture_action = None
            return False
        name = pygame_key_to_name(pg, key)
        if not name:
            return False
        # A key can only drive one input, so clear any previous owner rather
        # than leaving two inputs firing from the same key.
        for act, k in list(self.keymap.items()):
            if k == name and act != self.capture_action:
                del self.keymap[act]
        self.keymap[self.capture_action] = name
        self.capture_action = None
        return True

    def click(self, pos):
        """Resolve a click. Returns ('device'|'mode', value) if something was
        chosen, else None."""
        for rect, kind, value in self._hit:
            if rect.collidepoint(pos):
                if kind == "open":
                    self.open_menu = None if self.open_menu else "root"
                    return None
                if kind == "go":
                    self.open_menu = value       # navigate within the dropdown
                    self.capture_action = None
                    return None
                if kind == "bindkey":
                    self.capture_action = value  # next keypress binds this input
                    return None
                if kind == "keyreset":
                    if self.keymap is not None:
                        self.keymap.clear()
                        self.keymap.update(DEFAULT_KEYBOARD_BINDINGS)
                    self.capture_action = None
                    return ("keymap", None)
                self.open_menu = None
                return (kind, value)
        self.open_menu = None                    # click outside closes it
        return None


class VlcPreview:
    """Capture card rendered by VLC directly into the pygame window.

    This exists because piping raw frames through Python cannot reach 4K60:
    that is 1.49 GB/s as RGB, or 0.75 GB/s as NV12, before any per-frame Python
    work. VLC instead decodes on the GPU and draws straight into our window
    via set_hwnd(), so no video data crosses into Python at all - the frame
    rate is bounded by the GPU rather than by a pipe. It also plays the audio
    to the default Windows device itself, so no separate audio path is needed.

    The window handle is the pygame window's, so it is still the window Steam
    Input attaches to."""

    # One libvlc Instance, kept for the life of the process.
    # Releasing an Instance and building a new one on every device/mode change
    # deadlocks libvlc, which is what made switching capture modes hang. Only
    # the MediaPlayer is recreated per change.
    _instances = {}

    @classmethod
    def _instance(cls, vlc):
        # Keyed by the caching value so it is set from the same knob the media
        # options use; in practice that is fixed for the life of the process,
        # so there is still exactly one Instance.
        key = CAPTURE_LATENCY_MS
        inst = cls._instances.get(key)
        if inst is None:
            # live-caching is the one that matters: dshow:// is a live input,
            # so the file and network values never apply to it.
            args = [
                "--no-video-title-show",
                "--quiet",
                "--network-caching=0",
                f"--live-caching={CAPTURE_LATENCY_MS}",
                "--file-caching=0",
            ]
            inst = vlc.Instance(args)
            cls._instances[key] = inst
        return inst

    def __init__(self, hwnd, video_dev, audio_dev=None, mode=None,
                 record_path=None):
        self.error = None
        self._player = None
        self._inst = None
        self.record_path = record_path
        # Kept so fit() can tell a window that merely rounds off the source
        # shape from one that is genuinely a different shape.
        self.source_size = (mode[0], mode[1]) if mode and mode[1] else None
        try:
            import vlc
        except Exception:
            self.error = "python-vlc not installed (pip install python-vlc)"
            return
        try:
            # --no-xlib is harmless on Windows; the rest keeps latency down.
            self._inst = self._instance(vlc)
            self._player = self._inst.media_player_new()
            # The mode MUST be requested explicitly. DirectShow otherwise hands
            # over the first advertised format, which on this card is 640x480 -
            # that is why the picture looked low-res regardless of the source.
            mrl = "dshow://"
            opts = [f":dshow-vdev={video_dev}",
                    f":dshow-adev={audio_dev or 'none'}",
                    f":live-caching={CAPTURE_LATENCY_MS}"]
            if mode:
                w, h, fps, pixfmt = mode
                opts += [f":dshow-size={w}x{h}", f":dshow-fps={fps}"]
                # Only force a chroma when it is actually needed, i.e. to
                # select MJPEG - the one format carrying 4K60 / 1440p144 /
                # 1080p240, since uncompressed will not fit over USB at those
                # rates. For raw modes size+fps is enough and VLC negotiates
                # the rest; passing a fourcc there does more harm than good
                # (":dshow-chroma=NV12" is rejected and the stream never
                # starts, which is why raw modes appeared broken).
                chroma = {"mjpeg": "MJPG", "mjpg": "MJPG"}.get(pixfmt.lower())
                if chroma:
                    opts.append(f":dshow-chroma={chroma}")
            if record_path:
                # duplicate{} so the same stream both draws and writes. libvlc
                # 3.x has no record() call (that is 4.x), and the card cannot
                # be opened a second time by a separate recorder, so the split
                # has to happen inside this pipeline.
                #
                # No transcode: the frames are written in the codec they arrive
                # in. Re-encoding 4K60 in software would not keep up and would
                # cost picture quality for a recording that is meant to be a
                # faithful record. AVI because it carries MJPEG - what the high
                # modes actually deliver - without repackaging.
                dst = record_path.replace("\\", "/")
                opts += [":sout=#duplicate{dst=display,dst=std{access=file,"
                         'mux=avi,dst="%s"}}' % dst,
                         ":sout-keep"]
            media = self._inst.media_new(mrl, *opts)
            self._player.set_media(media)
            self._player.set_hwnd(hwnd)       # render into the pygame window
            self._player.play()
        except Exception as e:
            self.error = f"VLC failed to start ({e})"
            self.stop()

    def fit(self, window_size=None):
        """Fill the window without ever distorting the picture.

        Leaving the aspect ratio unset makes VLC letterbox to the source's own
        aspect, which leaves bars whenever the window is even slightly off that
        shape - and window chrome makes being a pixel or two off the normal
        case. Telling VLC the display aspect IS the window's aspect absorbs
        that rounding and fills edge to edge.

        That trick is only safe while the window really is the source's shape,
        which snap_window_to_aspect keeps it at. Fullscreen is the case where
        it cannot: the geometry is the monitor's, so on anything that is not
        the source's aspect - 16:10, ultrawide, a 16:9 monitor showing a 4:3
        source - claiming the window aspect stretches the picture to fill it.
        There the source aspect is used instead and VLC letterboxes to it."""
        try:
            self._player.video_set_scale(0.0)          # 0 = fit to window
            ratio = None
            if window_size and window_size[1]:
                w, h = int(window_size[0]), int(window_size[1])
                if self.source_size:
                    sw, sh = self.source_size
                    src = sw / sh
                    # 1%: comfortably more than the rounding snapping leaves,
                    # far less than the gap to the next standard aspect (16:9
                    # to 16:10 is 11%).
                    ratio = (f"{w}:{h}" if abs(w / h - src) <= 0.01 * src
                             else f"{sw}:{sh}")
                else:
                    # No mode was negotiated, so there is no source shape to
                    # compare against; let VLC letterbox to the stream's own.
                    ratio = None
            self._player.video_set_aspect_ratio(ratio)
        except Exception:
            pass

    def is_playing(self):
        try:
            return bool(self._player and self._player.is_playing())
        except Exception:
            return False

    def stop_async(self):
        """Begin tearing the player down; returns an Event set when finished.

        libvlc_media_player_stop() blocks until the embedded video output shuts
        down, and that vout needs its window's message loop to be pumped to get
        there. Our thread owns that pump, so calling stop() directly deadlocks:
        VLC waits for the message loop, the message loop waits for stop().

        Tearing down off-thread lets the caller keep pumping messages, which is
        what actually lets the shutdown complete. The shared Instance is never
        released here - only the player."""
        done = threading.Event()
        player, self._player = self._player, None

        def _teardown():
            try:
                if player:
                    player.stop()
                    player.release()
            except Exception:
                pass
            finally:
                done.set()

        if player is None:
            done.set()
        else:
            threading.Thread(target=_teardown, daemon=True).start()
        return done

    def stop(self):
        """Fire-and-forget teardown, for exit paths where nothing waits."""
        self.stop_async()


# --------------------------------------------------------------------------
# mpv backend: the same GPU-direct rendering as VLC, but with a shader hook.
# --------------------------------------------------------------------------

def mpv_exe(explicit=None):
    """Path to an mpv binary, or None.

    mpv is an external dependency exactly as VLC already is - python-vlc is
    only a binding and needs the VLC application installed. Nothing is
    bundled: a static mpv.exe is ~117MB, which has no business in a git
    repository, and GitHub refuses files that size anyway."""
    import shutil
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = shutil.which("mpv")
    if found:
        return found
    for p in (os.path.expandvars(r"%LOCALAPPDATA%\mpv\mpv.exe"),
              os.path.expandvars(r"%APPDATA%\mpv\mpv.exe"),
              r"C:\Program Files\mpv\mpv.exe"):
        if os.path.isfile(p):
            return p
    return None


def mpv_shader_dir():
    """Where mpv keeps user shaders, if that directory exists.

    Read rather than shipped: anyone using mpv seriously already has a shader
    collection here, and Anime4K in particular is the thing worth pointing at
    a capture card - it was built to restore detail in material that was
    rendered small and scaled up, which is exactly what a console does when
    the game's internal resolution is below its output."""
    for p in (os.path.expandvars(r"%APPDATA%\mpv\shaders"),
              os.path.expandvars(r"%LOCALAPPDATA%\mpv\shaders")):
        if os.path.isdir(p):
            return p
    return None


def resolve_shader(name):
    """Turn --shader into a path: a real path wins, else a name match.

    Matched loosely on purpose - the Anime4K filenames are long and exact
    ('Anime4K_Restore_CNN_M'), and nobody wants to type one correctly."""
    if not name:
        return None
    if os.path.isfile(name):
        return os.path.abspath(name)
    want = name.lower().replace(" ", "_")
    shaders = list_shaders()
    for label, path in shaders:
        if label.lower() == want:
            return path
    for label, path in shaders:
        if want in label.lower():
            return path
    return None


def list_shaders():
    """(label, absolute path) for every .glsl in the user's mpv shader dir."""
    d = mpv_shader_dir()
    if not d:
        return []
    try:
        names = sorted(n for n in os.listdir(d) if n.lower().endswith(".glsl"))
    except OSError:
        return []
    return [(n[:-5], os.path.join(d, n)) for n in names]


# What the scaler row offers. mpv's own names; ewa_lanczossharp is the one
# most mpv configs settle on, and is what this machine's mpv.conf already uses.
SCALER_CHOICES = ("bilinear", "spline36", "lanczos", "ewa_lanczos",
                  "ewa_lanczossharp")

# The downscale kernel - and on this setup that is the one that matters.
#
# Shrinking a 1440p feed into a smaller window IS supersampling, and mpv does
# it correctly out of the box: correct-downscaling (enough taps to actually
# average the detail away, rather than point-sampling it into aliasing) and
# linear-downscaling (average in linear light, so edges do not darken) are both
# on by default, verified by asking a running player rather than trusting the
# documentation. What is left to choose is the kernel.
#
# mpv's own default is hermite, which is cheap and sharp. mitchell is softer and
# averages more, which is what you want when the aim is to lose the stairsteps
# rather than to keep every pixel crisp - so that is the default here, with
# hermite one click away for anyone who disagrees.
DSCALE_CHOICES = ("hermite", "mitchell", "catmull_rom", "spline36", "lanczos",
                  "ewa_lanczossharp")


class MpvPreview:
    """Capture card rendered by mpv into the pygame window.

    Same shape as VlcPreview and the same core trick - hand a native window
    handle to a player that decodes and draws on the GPU, so no video data
    ever crosses into Python. The reason to have both is that mpv exposes a
    programmable shader stage and VLC does not: mpv can run Anime4K, FSR or
    any other GLSL shader on the frame between decode and display, which is
    the only way to attack aliasing that was baked into the signal before the
    capture card ever saw it.

    Driven as a subprocess over its JSON IPC pipe rather than through libmpv,
    because libmpv means shipping a ~100MB DLL and this needs neither a
    binding nor a build step - just mpv on the machine.

    Every IPC call is bounded and off the hot path: the input loop asks
    is_playing(), which only checks whether the process is alive, and the
    blocking request/response is used solely for toolbar clicks."""

    _seq = 0        # unique pipe name per player, so restarts never collide

    def __init__(self, hwnd, video_dev, audio_dev=None, mode=None,
                 record_path=None, exe=None, shader=None, scaler=None,
                 dscale=None):
        self.error = None
        self._proc = None
        self._ipc = None
        self._lock = threading.Lock()
        self._rid = 0
        self.source_size = (mode[0], mode[1]) if mode and mode[1] else None
        self.record_path = record_path
        self.shader = shader
        self.scaler = scaler
        self.dscale = dscale

        exe = mpv_exe(exe)
        if not exe:
            self.error = ("mpv not found - install it and put mpv.exe on PATH "
                          "(or pass --mpv-path)")
            return

        MpvPreview._seq += 1
        self._name = f"ounce-mpv-{os.getpid()}-{MpvPreview._seq}"
        self._pipe = r"\\.\pipe" "\\" + self._name

        # dshow through libavdevice. Size and rate must be requested or
        # DirectShow hands over its first advertised format, which on this
        # card is 640x480 - the same trap the VLC path documents.
        src = f"av://dshow:video={video_dev}"
        if audio_dev:
            src += f":audio={audio_dev}"
        lavf = ["fflags=nobuffer", "rtbufsize=256M"]
        if mode:
            w, h, fps, pixfmt = mode
            lavf += [f"video_size={w}x{h}", f"framerate={fps}"]
            # Only force a fourcc to select MJPEG, for the same reason as the
            # VLC path: raw modes negotiate fine on size+rate, and naming a
            # pixel format there stops the stream from ever starting.
            if pixfmt.lower() in ("mjpeg", "mjpg"):
                lavf.append("vcodec=mjpeg")

        args = [exe, f"--wid={hwnd}", f"--input-ipc-server={self._name}",
                # The user's own mpv.conf is deliberately not inherited. A
                # config tuned for watching video is the wrong one for playing
                # a game through: interpolation and display-resample both add
                # frames of latency, and this machine's mpv.conf sets each.
                # Shaders are still reachable - they are passed by full path.
                "--no-config",
                "--profile=low-latency",
                "--cache=no",
                "--demuxer-lavf-o=" + ",".join(lavf),
                "--no-osc", "--no-osd-bar", "--osd-level=0",
                "--no-input-default-bindings", "--input-vo-keyboard=no",
                "--no-border", "--keepaspect=yes",
                "--msg-level=all=error",
                "--idle=no", "--force-window=yes"]
        if not audio_dev:
            args.append("--audio=no")
        if scaler:
            args.append(f"--scale={scaler}")
        if dscale:
            # Stated rather than left to the default, and stated alongside the
            # two flags that make it supersampling rather than point sampling.
            args += [f"--dscale={dscale}",
                     "--correct-downscaling=yes", "--linear-downscaling=yes"]
        if shader:
            args.append(f"--glsl-shaders={shader}")
        if record_path:
            # mpv records the stream as it arrives, no transcode, the same
            # intent as VLC's duplicate{} - but built in rather than bolted on,
            # and switchable later through the same property.
            args.append(f"--stream-record={record_path}")
        args.append(src)

        try:
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          creationflags=_NO_WINDOW)
        except Exception as e:
            self.error = f"could not start mpv ({e})"
            return
        # Connecting waits on a pipe that only appears once mpv is up, so it
        # happens off-thread: the caller is the thread pumping window
        # messages, and mpv needs that pump to finish embedding itself.
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if self._proc is None or self._proc.poll() is not None:
                return
            try:
                f = open(self._pipe, "r+b", buffering=0)
            except OSError:
                time.sleep(0.1)
                continue
            with self._lock:
                self._ipc = f
            return

    def _command(self, *cmd, timeout=1.0):
        """One bounded request/response. None if mpv is not answering.

        Bounded on purpose: a player that has wedged must not be able to hang
        the client, and nothing here is worth waiting on."""
        import json
        with self._lock:
            f = self._ipc
            if f is None:
                return None
            self._rid += 1
            rid = self._rid
            try:
                f.write((json.dumps({"command": list(cmd),
                                     "request_id": rid}) + "\n").encode())
            except OSError:
                self._ipc = None
                return None
            end, buf = time.time() + timeout, b""
            while time.time() < end:
                try:
                    c = f.read(1)
                except OSError:
                    self._ipc = None
                    return None
                if not c:
                    return None
                buf += c
                if buf.endswith(b"\n"):
                    try:
                        d = json.loads(buf.decode(errors="replace"))
                    except ValueError:
                        d = None
                    buf = b""
                    # mpv interleaves async events with replies, so match the
                    # request id rather than taking the next line that arrives.
                    if d and d.get("request_id") == rid:
                        return d
            return None

    def set_shader(self, path):
        """Load one GLSL shader, or clear them all when path is falsy."""
        self.shader = path or None
        r = self._command("set_property", "glsl-shaders", path or "")
        return bool(r and r.get("error") == "success")

    def set_scaler(self, name):
        self.scaler = name
        r = self._command("set_property", "scale", name)
        return bool(r and r.get("error") == "success")

    def set_dscale(self, name):
        """The supersampling kernel: what averages the picture down into a
        window smaller than the source."""
        self.dscale = name
        r = self._command("set_property", "dscale", name)
        return bool(r and r.get("error") == "success")

    def supersampling(self, out_size):
        """(active, why) - is the picture actually being supersampled?

        Only true when the window has fewer pixels than the source. At 1:1
        there is nothing to average, so the honest answer is no, and saying so
        is better than implying a setting is doing work it cannot do.

        The output size is passed in rather than read back from mpv: embedded
        in a foreign window, osd-dimensions reports 100 whatever the window
        really is, and the client already knows the size it made the video
        child - measured, not guessed at over IPC."""
        out_w = out_size[0] if out_size else None
        src_w = self.source_size[0] if self.source_size else None
        if not out_w or not src_w:
            return None, "unknown"
        if out_w < src_w:
            return True, f"{src_w} -> {out_w} across, {src_w / out_w:.2f}x"
        if out_w == src_w:
            return False, f"1:1 at {src_w} - nothing to supersample"
        return False, f"{src_w} -> {out_w} across: upscaling, not supersampling"

    def set_recording(self, path):
        """Start or stop writing the stream. mpv toggles this live, so unlike
        the VLC path it needs no restart and cannot truncate what it wrote."""
        self.record_path = path or None
        r = self._command("set_property", "stream-record", path or "")
        return bool(r and r.get("error") == "success")

    def fit(self, window_size=None):
        """Nothing to do: embedded in a window, mpv tracks its parent's size
        on its own, and --keepaspect leaves the letterboxing correct on any
        window shape including fullscreen."""
        return

    def is_playing(self):
        # Process liveness only. This is called from the input loop, so it must
        # never do IPC - a bounded wait is still a wait, 500 times a second.
        return bool(self._proc and self._proc.poll() is None)

    def stop_async(self):
        """Begin teardown; returns an Event set when it is finished.

        Off-thread for the same reason as the VLC path: the caller owns the
        window message pump, and a player embedded in that window needs the
        pump to run in order to shut its video output down."""
        done = threading.Event()
        proc, self._proc = self._proc, None
        with self._lock:
            ipc, self._ipc = self._ipc, None

        def _teardown():
            try:
                if ipc:
                    try:
                        ipc.write(b'{"command":["quit"]}\n')
                    except OSError:
                        pass
                    try:
                        ipc.close()
                    except OSError:
                        pass
                if proc:
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
            except Exception:
                pass
            finally:
                done.set()

        if proc is None:
            done.set()
        else:
            threading.Thread(target=_teardown, daemon=True).start()
        return done

    def stop(self):
        self.stop_async()


def _explain_capture_error(stderr_text):
    """Turn ffmpeg's output into something actionable.

    ffmpeg's final line is usually just 'I/O error', while the useful line is
    further up - most often that the capture card is already open elsewhere,
    since DirectShow devices are exclusive."""
    lines = [l.strip() for l in (stderr_text or "").splitlines() if l.strip()]
    joined = " ".join(lines).lower()
    if "already in use" in joined or "could not run graph" in joined:
        return ("device is in use by another app - close Elgato Studio / "
                "4K Capture Utility / OBS and retry")
    if "no signal" in joined or "timeout" in joined:
        return "no signal from the capture card"
    if not lines:
        return "capture ended (no signal?)"
    # Prefer a line that actually names a problem over ffmpeg's generic tail.
    for l in reversed(lines):
        if "error opening input files" not in l.lower():
            return l[:100]
    return lines[-1][:100]


class CapturePreview:
    """Capture-card video decoded by ffmpeg on a background thread.

    The thread is the point: a frame read blocks for up to a frame period
    (~16ms at 60fps), which would wreck the 500Hz input loop if done inline.
    The loop only ever blits the most recent frame, so video can never gate
    input - the controller stays responsive even if capture stalls entirely."""

    def __init__(self, device_name, w=CAPTURE_W, h=CAPTURE_H, fps=30,
                 in_size=None, in_fps=None):
        self.error = None
        self.size = (w, h)
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc = None

        exe = ffmpeg_exe()
        if not exe:
            self.error = "ffmpeg not found (pip install imageio-ffmpeg)"
            return

        # Scale and rate-limit inside ffmpeg, never in Python. Two reasons:
        # ffmpeg does it far more cheaply, and it decides how much data crosses
        # the pipe. Raw RGB is bulky - 960x540 is ~1.5MB per frame, so 60fps
        # would be ~93MB/s through a Python pipe. Capping the preview at 30fps
        # halves that, and none of it affects input latency because the capture
        # runs on its own thread.
        cmd = [exe, "-hide_banner", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "dshow", "-rtbufsize", "256M"]
        # Optionally ask the card for a specific mode. A 4K60 or 1440p120
        # source costs real CPU to decode; requesting a smaller/slower mode
        # pushes that work onto the card instead.
        if in_size:
            cmd += ["-video_size", in_size]
        if in_fps:
            cmd += ["-framerate", str(in_fps)]
        # Every chain decrease-fits and pads rather than plain scaling: a bare
        # scale=w:h squashes any source that is not the pipe's shape (a 4:3 or
        # ultrawide input into a 16:9 pipe). The padding keeps every frame
        # exactly w*h*3 bytes, which the reader below depends on to reshape the
        # buffer - a chain emitting any other size would not raise, it would
        # slide the picture sideways forever.
        #
        # The source geometry is passed through so the upscaler can work out
        # its own intermediate sizes; without it the chain falls back to a
        # single resample, since there is nothing to compute a shrink from.
        src = None
        if in_size:
            try:
                src = tuple(int(x) for x in str(in_size).lower().split("x"))
            except Exception:
                src = None
        cmd += ["-i", f"video={device_name}",
                "-vf", upscale_chain(w, h, src),
                "-r", str(fps),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE,
                                          creationflags=_NO_WINDOW)
        except Exception as e:
            self.error = f"could not start ffmpeg ({e})"
            return
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        import numpy as np
        w, h = self.size
        nbytes = w * h * 3
        while not self._stop.is_set():
            try:
                raw = self._proc.stdout.read(nbytes)
            except Exception:
                break
            if not raw or len(raw) < nbytes:
                # Capture ended: usually no signal, or another app grabbed the
                # device. Surface it rather than showing a frozen frame.
                if not self._stop.is_set():
                    err = b""
                    try:
                        err = self._proc.stderr.read() or b""
                    except Exception:
                        pass
                    self.error = _explain_capture_error(err.decode("utf-8", "replace"))
                break
            try:
                arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
                arr = np.transpose(arr, (1, 0, 2))   # pygame surfaces are column-major
                with self._lock:
                    self._frame = arr
            except Exception:
                pass

    def blit_into(self, surface):
        """Draw the newest frame into the window, keeping its aspect ratio.

        Scaled to fit and centred rather than stretched to the window: the
        window is not always the frame's shape - fullscreen makes it the
        monitor's - and stretching to fill is what visibly distorts the
        picture there. Whatever the fit leaves over is filled black.

        True if a frame was drawn."""
        with self._lock:
            frame = self._frame
        if frame is None:
            return False
        try:
            surf = _pg.surfarray.make_surface(frame)
            sw, sh = surf.get_size()
            ww, wh = surface.get_size()
            if (sw, sh) != (ww, wh):
                scale = min(ww / sw, wh / sh)
                surf = _pg.transform.smoothscale(
                    surf, (max(1, int(sw * scale)), max(1, int(sh * scale))))
            fw, fh = surf.get_size()
            if (fw, fh) != (ww, wh):
                surface.fill((0, 0, 0))
            surface.blit(surf, ((ww - fw) // 2, (wh - fh) // 2))
            return True
        except Exception:
            return False

    def stop(self):
        self._stop.set()
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass


def split_audio_available():
    """Whether the separate audio pipe can actually run.

    Checked before VLC is told to skip audio, not after: under --split-audio
    VLC is given no audio device at all, so if CaptureAudio then fails to start
    there is nothing playing the sound and the card goes silent. Falling back to
    VLC's own audio is much better than that."""
    if not ffmpeg_exe():
        return False
    try:
        import sounddevice        # noqa: F401
    except Exception:
        return False
    return True


class CaptureAudio:
    """Capture-card audio piped from ffmpeg to the default Windows output.

    ffmpeg has no audio output device muxer on Windows (only the sdl2 video
    output), so it cannot play to a device by itself. Instead it decodes to raw
    PCM on stdout and sounddevice pushes that to whatever Windows has set as
    the default output."""

    def __init__(self, device_name, rate=48000, channels=2):
        self.error = None
        self._stop = threading.Event()
        self._proc = None
        self._stream = None
        self._rate = rate
        self._channels = channels
        self.underruns = 0
        self.ffmpeg_errors = 0

        exe = ffmpeg_exe()
        if not exe:
            self.error = "ffmpeg not found"
            return
        try:
            import sounddevice as sd
        except Exception:
            self.error = "sounddevice not installed (pip install sounddevice)"
            return

        cmd = [exe, "-hide_banner", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "dshow", "-rtbufsize", "16M",
               "-i", f"audio={device_name}",
               "-f", "s16le", "-acodec", "pcm_s16le",
               "-ar", str(rate), "-ac", str(channels), "-"]
        try:
            # stderr is read, not discarded. ffmpeg announces a starved or
            # overrun dshow buffer here ("real-time buffer ... too full",
            # "Thread message queue blocking"), and those are dropouts arriving
            # from the capture side rather than the output side - the two sound
            # identical and need opposite fixes, so the distinction has to be
            # visible rather than guessed at.
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE,
                                          creationflags=_NO_WINDOW)
            self._et = threading.Thread(target=self._drain_stderr, daemon=True)
            self._et.start()
            # No latency= here on purpose. It looks like the knob to reach for,
            # but sounddevice already defaults to 'high', which measured 213ms
            # of buffer on this machine - asking for AUDIO_LATENCY_MS instead
            # measured 107ms and would HALVE the room available. The buffer was
            # never the shortage; see _run() for what actually was.
            self._stream = sd.RawOutputStream(samplerate=rate, channels=channels,
                                              dtype="int16", blocksize=1024)
            self._stream.start()
        except Exception as e:
            self.error = f"audio start failed ({e})"
            self.stop()
            return

        # What the output actually gave us, rather than what was asked for.
        # sounddevice's 'high' default is the whole buffer here, and it differs
        # per device and host API - printing it is how a machine where it is
        # far smaller than this one's 213ms becomes obvious instead of puzzling.
        try:
            print(f"    audio pipe: {self._stream.latency * 1000:.0f}ms device "
                  f"buffer, {AUDIO_LATENCY_MS}ms primed ahead")
        except Exception:
            pass

        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _drain_stderr(self):
        """Surface ffmpeg's complaints; they are the capture-side dropouts."""
        shown = 0
        while not self._stop.is_set():
            try:
                line = self._proc.stderr.readline()
            except Exception:
                break
            if not line:
                break
            msg = line.decode("utf-8", errors="ignore").strip()
            if not msg:
                continue
            self.ffmpeg_errors += 1
            # First few only: a card that is dropping does so continuously, and
            # scrolling the console would cost more time than it reports.
            if shown < 5:
                shown += 1
                print(f"[!] capture audio (ffmpeg): {msg}")
                if shown == 5:
                    print("    ...further ffmpeg audio messages suppressed.")

    def _run(self):
        # Prime the output with silence before feeding it anything real.
        #
        # ffmpeg is reading a live capture, so it delivers audio at exactly real
        # time and the device consumes it at exactly real time. With nothing
        # queued ahead, the pipeline runs at zero margin forever - any
        # scheduling delay in this thread means the device wanted samples that
        # had not arrived, and a starved output does not play late, it crackles.
        # No buffer size alone fixes that: a bigger buffer never fills, because
        # the producer is never ahead. The queue has to be given a head start,
        # which is what this silence is, and it is exactly what VLC's
        # live-caching was quietly doing before audio was split out of it.
        cushion = int(self._rate * AUDIO_LATENCY_MS / 1000.0)
        if cushion:
            try:
                self._stream.write(bytes(cushion * self._channels * 2))
            except Exception:
                pass

        chunk = 1024 * self._channels * 2   # frames * channels * bytes/sample
        warned = False
        while not self._stop.is_set():
            try:
                data = self._proc.stdout.read(chunk)
                if not data:
                    break
                # write() reports whether the device ran dry waiting for this.
                # write() is documented to return whether the device ran dry.
                # Treat a report as a useful hint, never as proof of health:
                # PortAudio did not surface it at all through the blocking API
                # on this host, so silence here does not mean silence there.
                if self._stream.write(data):
                    self.underruns += 1
                    if self.underruns == 25 and not warned:
                        warned = True
                        print(f"[!] Capture audio is running dry at "
                              f"{AUDIO_LATENCY_MS}ms - raise --audio-latency.")
            except Exception:
                break

    def stop(self):
        self._stop.set()
        for obj, meth in ((self._proc, "kill"), (self._stream, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass


_fullscreen = False
_saved_window = None       # (style, exstyle, x, y, w, h) to restore on exit

TOOLBAR_H = 26             # collapsed strip height; the menu drops down over it
_video_child = None        # HWND VLC renders into (a child of the pygame window)


def create_video_child(parent_hwnd):
    """A child window for VLC to render into.

    VLC draws directly into whatever HWND it is given, taking over every pixel
    of it - so pointing it at the pygame window leaves nowhere to draw a
    toolbar. Giving it a child window instead lets us position the video below
    a strip that pygame still owns. Uses the built-in STATIC class so no window
    class has to be registered."""
    global _video_child
    try:
        import ctypes
        u = ctypes.windll.user32
        WS_CHILD, WS_VISIBLE, WS_CLIPSIBLINGS = 0x40000000, 0x10000000, 0x04000000
        _video_child = u.CreateWindowExW(
            0, "STATIC", None,
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            0, TOOLBAR_H, 100, 100,
            parent_hwnd, None, None, None)
        return _video_child
    except Exception:
        _video_child = None
        return None


def layout_video_child(show_toolbar):
    """Position the video child under the toolbar, or over the whole window."""
    if not _video_child or _window is None or _pg is None:
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        cw, ch = window_client_size() or _window.get_size()
        top = TOOLBAR_H if show_toolbar else 0
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        u.SetWindowPos(_video_child, 0, 0, top, cw, max(1, ch - top),
                       SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


def show_video_child(visible):
    """Show or hide the video child window.

    A child window always paints over its parent's client area, so anything
    pygame draws underneath it - the entire dropdown - is invisible no matter
    what order we draw in. Hiding the video while a menu is open is what makes
    the menu visible and clickable at all."""
    if not _video_child:
        return
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(_video_child, 5 if visible else 0)  # SW_SHOW/SW_HIDE
    except Exception:
        pass


def video_child_size(show_toolbar):
    cw, ch = window_client_size() or (WINDOW_W, WINDOW_H)
    top = TOOLBAR_H if show_toolbar else 0
    return (cw, max(1, ch - top))


def is_fullscreen():
    return _fullscreen


def toggle_borderless_fullscreen(force=None):
    """Borderless fullscreen on the monitor the window is currently on.

    Done with Win32 style changes rather than pygame's fullscreen: SDL's
    fullscreen path recreates the window, which would invalidate the HWND VLC
    renders into and kill the video. Stripping the frame and resizing in place
    keeps the same window, so playback continues uninterrupted.

    Borderless (WS_POPUP) rather than exclusive fullscreen also means alt-tab
    and the Steam overlay keep working, and focus - which Steam Input depends
    on - behaves normally."""
    global _fullscreen, _saved_window
    if _window is None or _pg is None:
        return
    want = (not _fullscreen) if force is None else bool(force)
    if want == _fullscreen:
        return
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = _pg.display.get_wm_info().get("window")
        if not hwnd:
            return

        GWL_STYLE, GWL_EXSTYLE = -16, -20
        WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
        SWP_FRAMECHANGED, SWP_NOZORDER = 0x0020, 0x0004
        SWP_SHOWWINDOW = 0x0040

        if want:
            style = u.GetWindowLongW(hwnd, GWL_STYLE)
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            r = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            _saved_window = (style, ex, r.left, r.top,
                             r.right - r.left, r.bottom - r.top)

            # Bounds of the monitor this window is on, so it fullscreens on the
            # right display in a multi-monitor setup.
            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            mon = u.MonitorFromWindow(hwnd, 2)      # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            u.GetMonitorInfoW(mon, ctypes.byref(mi))
            m = mi.rcMonitor

            u.SetWindowLongW(hwnd, GWL_STYLE, WS_POPUP | WS_VISIBLE)
            u.SetWindowPos(hwnd, 0, m.left, m.top,
                           m.right - m.left, m.bottom - m.top,
                           SWP_FRAMECHANGED | SWP_NOZORDER | SWP_SHOWWINDOW)
            _fullscreen = True
        else:
            if _saved_window:
                style, ex, x, y, w, h = _saved_window
                u.SetWindowLongW(hwnd, GWL_STYLE, style)
                u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
                u.SetWindowPos(hwnd, 0, x, y, w, h,
                               SWP_FRAMECHANGED | SWP_NOZORDER | SWP_SHOWWINDOW)
            _fullscreen = False
    except Exception:
        pass


def window_client_size():
    """The window's client area in real pixels.

    This is what VLC actually draws into - not the pygame surface size and not
    the outer window rect, both of which can disagree with it once title bar
    and borders are involved."""
    if _window is None or _pg is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _pg.display.get_wm_info().get("window")
        if not hwnd:
            return _window.get_size()
        cr = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(cr))
        w, h = cr.right - cr.left, cr.bottom - cr.top
        return (w, h) if w > 0 and h > 0 else _window.get_size()
    except Exception:
        return _window.get_size()


def snap_window_to_aspect(aspect, extra_h=0):
    """Force the window so the VIDEO area is `aspect`, keeping its width.

    extra_h is space the video does not get - the toolbar strip. Sizing the
    whole client to 16:9 and then carving the toolbar out of it leaves the
    video area at the wrong shape, which is exactly how black bars come back.

    Resizing via Win32 rather than pygame.display.set_mode() is deliberate:
    set_mode can recreate the window and invalidate the HWND that VLC is
    rendering into. This keeps the same window and just changes its size, so
    the capture keeps filling it exactly instead of being letterboxed or
    stretched when the shape drifts off 16:9."""
    if _window is None or _pg is None or not aspect:
        return
    if _fullscreen:
        return          # fullscreen owns the geometry; do not fight it
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _pg.display.get_wm_info().get("window")
        if not hwnd:
            return
        u = ctypes.windll.user32
        wr, cr = wintypes.RECT(), wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(wr))
        u.GetClientRect(hwnd, ctypes.byref(cr))
        chrome_w = (wr.right - wr.left) - (cr.right - cr.left)
        chrome_h = (wr.bottom - wr.top) - (cr.bottom - cr.top)

        client_w = cr.right - cr.left
        want_client_h = int(round(client_w / aspect)) + int(extra_h)
        if abs(want_client_h - (cr.bottom - cr.top)) <= 2:
            return                        # already the right shape
        SWP_NOMOVE, SWP_NOZORDER = 0x0002, 0x0004
        u.SetWindowPos(hwnd, 0, 0, 0,
                       client_w + chrome_w, want_client_h + chrome_h,
                       SWP_NOMOVE | SWP_NOZORDER)
    except Exception:
        pass


def set_window_size(w, h):
    global WINDOW_W, WINDOW_H
    WINDOW_W, WINDOW_H = max(320, w), max(180, h)


def _open_status_window(pygame):
    """Small always-there window so Steam Input has something to attach to.

    Steam decides which controller configuration to apply based on the focused
    window. Without one it keeps the pad in Desktop (keyboard/mouse) mode, so
    this window is functional, not decorative - it is what puts the controller
    into game mode."""
    global _window
    pygame.display.set_caption("Ounce Bridge - keep focused for Steam Input")
    # Before set_mode(), or SDL has already created the window with its default
    # icon. PNG rather than the .ico the exe carries: SDL_image rejects
    # PNG-compressed ICO frames, which is what a modern multi-size .ico is.
    try:
        pygame.display.set_icon(pygame.image.load(bundled_path("ounce_icon.png")))
    except Exception:
        pass   # decorative only - never keep the window from opening over it
    # Sized for the capture preview: this is the same window Steam Input needs
    # focused, so the game view and the focus target are one and the same.
    _window = pygame.display.set_mode((WINDOW_W, WINDOW_H), pygame.RESIZABLE)
    _draw_status("starting...")

    # Raise and focus ourselves so the controller switches out of Desktop mode
    # without the user having to click the window first.
    try:
        import ctypes
        hwnd = pygame.display.get_wm_info().get("window")
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 5)          # SW_SHOW
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass   # focus is a nicety; the window itself is what matters


def _draw_status(line, extra=()):
    """Repaint the status window.

    With preview off (the default) this window's only job is to be the thing
    Steam Input attaches to - so it may as well show what the bridge is doing.
    Video should come from the capture card's HDMI passthrough, which is
    lag-free hardware and far better than anything drawn here."""
    if _window is None or _pg is None:
        return
    try:
        w, h = _window.get_size()
        _window.fill((16, 16, 20))
        big = _pg.font.SysFont("consolas", 22)
        mid = _pg.font.SysFont("consolas", 15)
        small = _pg.font.SysFont("consolas", 13)

        _window.blit(big.render("Ounce Bridge", True, (235, 235, 245)), (20, 18))
        _window.blit(mid.render(line, True, (140, 220, 160)), (20, 54))

        y = 92
        for s in extra:
            _window.blit(small.render(s, True, (190, 190, 205)), (20, y))
            y += 20

        y = max(y + 10, h - 62)
        _window.blit(small.render("Keep this window focused - Steam applies its game",
                                  True, (120, 120, 140)), (20, y))
        _window.blit(small.render("controller layout to whichever window has focus.",
                                  True, (120, 120, 140)), (20, y + 18))
        _pg.display.flip()
    except Exception:
        pass


def pump_window(vlc_active=False, on_resize=None, on_click=None, on_key=None):
    """Service the window's message queue so Windows does not mark it as
    'not responding', which would also drop it out of the foreground.

    Also handles resizing. With VLC we must NOT call set_mode() again: that
    can recreate the window and invalidate the HWND VLC is drawing into.
    VLC follows the window on its own, so resizing just works."""
    global _window
    if _window is None or _pg is None:
        return True
    try:
        for e in _pg.event.get():
            if e.type == _pg.QUIT:
                return False
            if e.type == _pg.VIDEORESIZE:
                if vlc_active:
                    # Do NOT call set_mode here: it can recreate the window and
                    # invalidate the HWND VLC draws into. VLC follows the
                    # window itself; it just needs re-fitting to the new shape.
                    if on_resize:
                        on_resize()
                else:
                    _window = _pg.display.set_mode((max(320, e.w), max(180, e.h)),
                                                   _pg.RESIZABLE)
            if e.type == _pg.MOUSEBUTTONDOWN and e.button == 1 and on_click:
                on_click(e.pos)
            # Key capture wins over the shortcuts below, so binding F11 or Esc
            # to a controller input is possible instead of being swallowed.
            if e.type == _pg.KEYDOWN and on_key and on_key(e.key):
                continue
            if e.type == _pg.KEYDOWN and e.key in (_pg.K_F11,):
                toggle_borderless_fullscreen()
                if on_resize:
                    on_resize()
            if e.type == _pg.KEYDOWN and e.key == _pg.K_ESCAPE and is_fullscreen():
                toggle_borderless_fullscreen(False)
                if on_resize:
                    on_resize()
    except Exception:
        pass
    return True


def gamepad_available():
    global _pg, _sdl_controller, _under_steam
    if _pg is not None:
        return True
    try:
        import os
        os.environ.setdefault("SDL_JOYSTICK_HIDAPI_PS5", "1")   # native DualSense

        # Steam sets these in the environment of anything it launches. When we
        # are running under Steam, Steam Input owns the pad and presents it as
        # a virtual gamepad - so SDL's direct Steam Controller HID driver must
        # be OFF, or it claims the device first and the layout configured in
        # Steam silently does nothing. Standalone, we want the opposite, so the
        # Steam Controller is usable at all.
        under_steam = any(os.environ.get(v) for v in
                          ("SteamAppId", "SteamGameId", "SteamClientLaunch",
                           "SteamOverlayGameId", "SteamEnv"))
        _under_steam = under_steam      # pad_name uses it to spot Steam's pad
        os.environ.setdefault("SDL_JOYSTICK_HIDAPI_STEAM",
                              "0" if under_steam else "1")
        if under_steam:
            os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")

        # A window is wanted unless explicitly refused (--no-window clears the
        # variable). Under Steam it is not optional: Steam switches a controller
        # out of its Desktop (keyboard/mouse) configuration when it detects the
        # *game*, which it does by hooking a real window. A windowless process
        # never triggers that switch, leaving the pad stuck in mouse mode no
        # matter what layout is configured.
        want_window = under_steam or os.environ.get("OUNCE_WINDOW") == "1"
        if not want_window:
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

        import pygame
        from pygame._sdl2 import controller as sdlcontroller
        pygame.init()
        sdlcontroller.init()
        if want_window:
            try:
                _open_status_window(pygame)
            except Exception as e:
                print(f"[!] Could not create a window ({e}); continuing headless.")
        _pg, _sdl_controller = pygame, sdlcontroller
        return True
    except Exception as e:
        print(f"[-] Controller support unavailable ({e}). Install with: pip install pygame")
        return False


def list_controllers():
    if not gamepad_available():
        return []
    out = []
    for i in range(_pg.joystick.get_count()):
        j = _pg.joystick.Joystick(i)
        j.init()
        out.append((i, pad_name(i), _sdl_controller.is_controller(i)))
    return out


def open_controller(index=None):
    """Open a controller. index=None auto-picks the first suitable one."""
    if not gamepad_available():
        return None
    count = _pg.joystick.get_count()
    if count == 0:
        print("[-] No controllers detected.")
        return None

    if index is None:
        for i in range(count):
            name = _pg.joystick.Joystick(i).get_name()
            if name.strip().lower() in OUNCE_SELF_NAMES:
                print(f"[!] Skipping controller {i} ('{name}') - that is the Ounce")
                print(f"    target itself; using it would feed our own output back in.")
                print(f"    Pass --controller {i} to override.")
                continue
            if _sdl_controller.is_controller(i):
                index = i
                break
        if index is None:
            print("[-] No usable controller found (only the Ounce target is present).")
            return None

    if not _sdl_controller.is_controller(index):
        print(f"[-] Controller {index} has no SDL GameController mapping; "
              f"its buttons would be arbitrary. Refusing to guess.")
        return None

    c = _sdl_controller.Controller(index)
    c.init()
    print(f"[+] Using controller {index}: {pad_name(index)}")
    return c


def _axis_to_byte(v, deadzone):
    """SDL axis (-32768..32767) -> protocol byte (0..255, centre 128).
    SDL's Y axes are positive-down, which already matches this protocol
    (W/up sends 0), so no inversion is needed."""
    if -deadzone < v < deadzone:
        return 128
    b = int((v + 32768) * 255 / 65535 + 0.5)
    return 0 if b < 0 else (255 if b > 255 else b)


def _pad_button(c, name):
    """Read an SDL GameController button by name. Some buttons (MISC1 / the
    Share-Capture key) do not exist on every pad, so this fails soft."""
    try:
        return bool(c.get_button(getattr(_pg, "CONTROLLER_BUTTON_" + name)))
    except Exception:
        return False


def read_controller(c, deadzone, trigger_threshold, button_bindings=None):
    """Read a controller into (buttons, lx, ly, rx, ry, aux).

    button_bindings maps an action ('B1', 'HOME', 'LX-', ...) to an SDL
    GameController button name, so a slot can rebind the pad however it likes.
    Analog sticks always drive the four axes; digital axis actions (LX- etc.)
    may additionally be bound to buttons, e.g. to use a d-pad as a stick."""
    P = _pg
    _pg.event.pump()
    bindings = DEFAULT_PAD_BUTTONS if button_bindings is None else button_bindings

    b = 0
    aux = 0
    axes = [128, 128, 128, 128]
    for action, btn in bindings.items():
        if not _pad_button(c, str(btn).upper()):
            continue
        if action in BUTTON_ACTIONS:
            b |= BUTTON_ACTIONS[action]
        elif action in AUX_ACTIONS:
            aux |= AUX_ACTIONS[action]
        elif action in AXIS_ACTIONS:
            idx, val = AXIS_ACTIONS[action]
            axes[idx] = val

    # The Switch Pro's ZL/ZR are digital, so analog triggers get thresholded.
    try:
        if c.get_axis(P.CONTROLLER_AXIS_TRIGGERLEFT) > trigger_threshold:
            b |= SPI_MASK_L2
        if c.get_axis(P.CONTROLLER_AXIS_TRIGGERRIGHT) > trigger_threshold:
            b |= SPI_MASK_R2
    except Exception:
        pass

    # Analog sticks win over any digital axis binding that is not pressed.
    stick = (
        _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_LEFTX), deadzone),
        _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_LEFTY), deadzone),
        _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_RIGHTX), deadzone),
        _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_RIGHTY), deadzone),
    )
    axes = [_merge_axis(axes[i], stick[i]) for i in range(4)]
    return b, axes[0], axes[1], axes[2], axes[3], aux

def calculate_crc8(data):
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc

def make_serial_packet(target_id, buttons, lx, ly, rx, ry, aux=0, enabled_mask=0x1):
    # Bits 2..5 restate which slots are enabled in every packet, so the master
    # re-syncs after any dropped packet and slots can be toggled at any time.
    flags = ((target_id & SPI_TARGET_ID_MASK)
             | ((enabled_mask << SPI_ENABLED_SHIFT) & SPI_ENABLED_MASK)
             | aux)
    payload = struct.pack('<BBHBBBB', 0x5A, flags, buttons, lx, ly, rx, ry)
    crc = calculate_crc8(payload)
    return payload + bytes([crc])

def find_primary_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        desc = port.description.lower()
        dev = port.device.lower()
        if "bluetooth" in desc or "bth" in dev or "standard serial" in desc:
            continue
        if "pico" in desc or "rp2040" in desc or "rp2350" in desc or "usb serial device" in desc:
            return port.device
    for port in ports:
        desc = port.description.lower()
        if "bluetooth" not in desc and "standard serial" not in desc:
            return port.device
    return 'COM5'

def main():
    parser = argparse.ArgumentParser(description="Ounce RP2350 Master Real-Time Keyboard Bridge")
    parser.add_argument("--port", type=str, default=None, help="Serial port of Primary RP2350 Master")
    parser.add_argument("--target", type=int, default=0, help="Target Slave ID (0..3)")
    parser.add_argument("--max-packets", type=int, default=0, help="Exit automatically after transmitting N packets (0 = run continuously)")
    parser.add_argument("--config", type=str, default=None,
                        help="JSON file defining per-slot key/button bindings. "
                             "Lets each virtual Switch controller be driven by "
                             "whatever keys or pad buttons you choose.")
    parser.add_argument("--dump-config", type=str, default=None, metavar="FILE",
                        help="Write a commented default config to FILE and exit.")
    parser.add_argument("--assign", action="append", default=[], metavar="SLOT=SOURCE",
                        help="Assign an input to a player slot, e.g. --assign 0=keyboard "
                             "--assign 1=pad:1 --assign 2=pad:DualSense. Repeatable; "
                             "assigning several sources to one slot merges them. "
                             "Default: slot 0 gets the keyboard plus the first controller.")
    parser.add_argument("--input", choices=["auto", "keyboard", "gamepad"], default="auto",
                        help="Legacy single-slot mode when no --assign is given.")
    parser.add_argument("--controller", type=int, default=None,
                        help="Controller index for legacy single-slot mode (see --list-controllers).")
    parser.add_argument("--list-controllers", action="store_true",
                        help="List detected controllers and exit.")
    parser.add_argument("--capture", default=None, metavar="DEV",
                        help="Show this capture device in the window (index or name "
                             "substring, e.g. --capture Elgato). Default: auto-pick a "
                             "capture card when a window is shown.")
    parser.add_argument("--capture-audio", default=None, metavar="DEV",
                        help="Play this capture device's audio to the default Windows "
                             "output. Default: the matching audio device.")
    parser.add_argument("--no-preview", action="store_true",
                        help="Do not show the capture card in the window. Worth doing if "
                             "you already watch the card's HDMI passthrough on a TV - "
                             "that path is lag-free hardware and never touches the PC.")
    parser.add_argument("--preview", action="store_true",
                        help="(default) Show the capture card in the window.")
    parser.add_argument("--no-capture", action="store_true",
                        help="Alias for --no-preview.")
    parser.add_argument("--video-backend", choices=["vlc", "mpv", "ffmpeg"],
                        default="vlc",
                        help="How the capture card is drawn. 'vlc' renders on the GPU "
                             "straight into the window (handles 4K60, plays its own "
                             "audio). 'mpv' does the same but with a programmable "
                             "shader stage, so Anime4K and friends can run on the "
                             "picture - needs mpv.exe on PATH. 'ffmpeg' pipes raw "
                             "frames through Python, which "
                             "measures ~94fps at 1440p60 and ~175 at 1080p60 with "
                             "the upscaler on, so it is not the ceiling this once "
                             "said it was - but it adds a pipe hop VLC does not "
                             "have. Default vlc, falling back to ffmpeg. The "
                             "upscaler only exists here: VLC draws on the GPU and "
                             "never lets a filter chain near the frames.")
    parser.add_argument("--capture-size", default="auto", metavar="WxH",
                        help="Preview pipe size. Raw frames cross a pipe, so this sets "
                             "the bandwidth: 960x540 is ~1.5MB/frame. Default auto - "
                             "960x540 normally, or the window size when --upscale is on, "
                             "so the picture is resampled once, at the size it is shown "
                             "at, instead of twice.")
    parser.add_argument("--capture-fps", type=int, default=30, metavar="N",
                        help="Preview frame rate cap. Default 30; 60 doubles pipe "
                             "bandwidth, and is worth it with --upscale.")
    parser.add_argument("--mpv-path", default=None, metavar="PATH",
                        help="Where mpv.exe is, if it is not on PATH. Nothing is "
                             "bundled - a static mpv.exe is ~117MB and has no place "
                             "in a git repository.")
    parser.add_argument("--shader", default=None, metavar="NAME|PATH",
                        help="GLSL shader for the mpv backend: a full path, or the "
                             "name of a .glsl in your mpv shaders folder. Anime4K's "
                             "Restore_CNN shaders are the ones worth trying on a "
                             "console feed - they were built to rebuild detail in "
                             "material rendered small and scaled up, which is what a "
                             "console does when the game's internal resolution is "
                             "below its output. --list-shaders shows what you have.")
    parser.add_argument("--scaler", default="ewa_lanczossharp",
                        choices=SCALER_CHOICES,
                        help="mpv's scaling kernel. Only does anything when the "
                             "window is a different size from the source. Default "
                             "ewa_lanczossharp.")
    parser.add_argument("--dscale", default="mitchell", choices=DSCALE_CHOICES,
                        help="Downscale kernel for the mpv backend - the "
                             "supersampling knob. Shrinking the 1440p feed into a "
                             "smaller window IS supersampling, and this is what "
                             "does the averaging. Default mitchell (softer, loses "
                             "stairsteps); mpv's own default hermite is sharper. "
                             "Does nothing at 1:1, where there is nothing to "
                             "average.")
    parser.add_argument("--list-shaders", action="store_true",
                        help="List the GLSL shaders found in your mpv shaders "
                             "folder, and exit.")
    parser.add_argument("--upscale", choices=UPSCALE_CHOICES, default="off",
                        help="Internal upscaler, ffmpeg backend only. 'aa' resamples "
                             "down to --render-height and back up to the window, both "
                             "passes in linear light: that is supersampling, and it is "
                             "what makes a shrunken window look clean, applied at full "
                             "size. 'fast' is one good-kernel resample with no shrink. "
                             "Default off.")
    parser.add_argument("--render-height", type=int, default=UPSCALE_RENDER_H,
                        metavar="H",
                        help="How far --upscale=aa shrinks before scaling back up. "
                             "Lower is stronger antialiasing and a softer picture. "
                             f"0 disables the shrink. Default {UPSCALE_RENDER_H}.")
    parser.add_argument("--sharpen", type=float, default=UPSCALE_SHARPEN,
                        metavar="S",
                        help="Contrast-adaptive sharpening after the resample, 0.0-1.0, "
                             f"to put back the bite it costs. 0 = none. Default "
                             f"{UPSCALE_SHARPEN}.")
    parser.add_argument("--capture-mode", default=None, metavar="WxH[@FPS]",
                        help="Capture mode to request, e.g. 3840x2160@30 or 1920x1080@120. "
                             "Default: the highest pixels-per-second the card offers. "
                             "DirectShow gives you 640x480 unless a mode is requested.")
    parser.add_argument("--capture-latency", type=int, default=CAPTURE_LATENCY_MS,
                        metavar="MS",
                        help="How much the VLC path may buffer, in ms. Default 20, which "
                             "buffers the picture only because audio is split out of VLC "
                             "by default. With --no-split-audio this buffers the sound "
                             "too and 20 will crackle - raise it to 100-200 there.")
    parser.add_argument("--audio-latency", type=int, default=AUDIO_LATENCY_MS,
                        metavar="MS",
                        help="How far ahead the split-audio pipe queues sound, in ms. "
                             "Default 100. This delays only the audio, never the "
                             "picture, so raise it if you hear crackling - that is the "
                             "output running dry. No effect with --no-split-audio.")
    parser.add_argument("--split-audio", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Play capture audio through a separate ffmpeg pipe rather "
                             "than through VLC, so --capture-latency buffers only the "
                             "video (default). The buffer exists for the audio output's "
                             "sake, so taking audio out of VLC is what lets the picture "
                             "run low-latency without crackling. --no-split-audio puts "
                             "it back through VLC. Recordings take their audio from VLC "
                             "either way.")
    parser.add_argument("--list-modes", action="store_true",
                        help="List the capture card's supported modes and exit.")
    parser.add_argument("--capture-input-size", default=None, metavar="WxH",
                        help="Ask the card for this input mode (e.g. 1920x1080). A 4K60 "
                             "or 1440p120 source costs real CPU to decode; requesting a "
                             "smaller mode moves that work onto the card.")
    parser.add_argument("--capture-input-fps", type=int, default=None, metavar="N",
                        help="Ask the card for this input frame rate (e.g. 30 or 60).")
    parser.add_argument("--list-capture", action="store_true",
                        help="List capture devices ffmpeg can see and exit.")
    parser.add_argument("--window", action="store_true",
                        help="Deprecated - the window is on by default. Kept so "
                             "existing shortcuts and Steam launch options still work.")
    parser.add_argument("--no-window", action="store_true",
                        help="Run headless: no window, so no video, toolbar or "
                             "remapping, and slots are chosen at a console prompt.")
    parser.add_argument("--fullscreen", action="store_true",
                        help="Start in borderless fullscreen. F11 toggles it at any "
                             "time, Esc leaves it.")
    parser.add_argument("--window-size", default=f"{WINDOW_W}x{WINDOW_H}", metavar="WxH",
                        help="Preview window size. With the VLC backend this IS the "
                             "display resolution, so make it as large as you want the "
                             "picture. Default 1280x720. The window is resizable.")
    parser.add_argument("--probe", action="store_true",
                        help="Show live axis/button/hat values from the controller and "
                             "exit. Run this through Steam to see what Steam Input is "
                             "actually sending.")
    parser.add_argument("--no-interactive", action="store_true",
                        help="Skip the interactive slot setup and use the legacy "
                             "single-slot default. Prompting is skipped automatically "
                             "when input is not a terminal.")
    parser.add_argument("--deadzone", type=int, default=3000,
                        help="Stick deadzone in SDL axis units (0..32767). Default 3000.")
    parser.add_argument("--trigger-threshold", type=float, default=0.5,
                        help="Analog trigger level counted as a ZL/ZR press (0..1). Default 0.5.")
    args = parser.parse_args()
    set_capture_latency(args.capture_latency)
    set_audio_latency(args.audio_latency)
    set_upscale(args.upscale)
    set_render_height(args.render_height)
    set_sharpen(args.sharpen)

    if args.dump_config:
        write_default_config(args.dump_config)
        print(f"[+] Wrote default config to {args.dump_config}")
        print("    Edit it, then run with --config " + args.dump_config)
        sys.exit(0)

    # The window IS the client: the video, the toolbar, the slot switches and
    # the key remapper all live in it. So it is the default, and --no-window is
    # the opt-out for a headless console bridge. It used to be the other way
    # round, from when the only reason for a window was to give Steam Input
    # something to hook - which meant running the exe directly got none of the
    # interface.
    if args.no_window:
        os.environ.pop("OUNCE_WINDOW", None)   # beat an inherited OUNCE_WINDOW=1
    else:
        os.environ["OUNCE_WINDOW"] = "1"

    # Bring pygame up now if a window is wanted. It is otherwise only started
    # as a side effect of opening a controller, so a keyboard-only setup would
    # silently get no window - and therefore no toolbar, no capture, and no
    # Steam Input attachment.
    if (os.environ.get("OUNCE_WINDOW") == "1" or os.environ.get("SteamAppId")
            or os.environ.get("SteamGameId")):
        gamepad_available()
    if args.window_size:
        try:
            w, h = (int(x) for x in args.window_size.lower().split("x"))
            set_window_size(w, h)
        except Exception:
            print(f"[-] Bad --window-size '{args.window_size}', using default")

    if args.list_shaders:
        found = mpv_exe(args.mpv_path)
        print(f"mpv: {found or 'NOT FOUND - install it and put mpv.exe on PATH'}")
        d = mpv_shader_dir()
        print(f"shader folder: {d or 'none (looked in %APPDATA%/mpv/shaders)'}")
        shaders = list_shaders()
        if not shaders:
            print("   no .glsl files found - drop shaders in that folder")
        for label, _p in shaders:
            print(f"   {label}")
        sys.exit(0)

    if args.list_capture:
        vids, auds = list_dshow_devices()
        if not vids and not auds:
            print("[-] ffmpeg found no capture devices "
                  "(is ffmpeg installed? pip install imageio-ffmpeg)")
        print("Video:")
        for i, n in enumerate(vids):
            print(f"   [{i}] {n}")
        print("Audio:")
        for i, n in enumerate(auds):
            print(f"   [{i}] {n}")
        sys.exit(0)

    if args.list_modes:
        vids, _ = list_dshow_devices()
        dev = pick_capture(args.capture, vids)
        if not dev:
            print("[-] No capture device found.")
            sys.exit(1)
        modes = list_dshow_modes(dev)
        best = pick_best_mode(modes)
        print(f"Modes for {dev}:")
        for w, h, fps, pf in sorted(modes, key=lambda x: -(x[0] * x[1] * x[2])):
            star = "  <- default (highest resolution)" if (w, h, fps, pf) == best else ""
            print(f"   {w}x{h} @{fps:>3}fps  {pf}{star}")
        print("\nSelect one with --capture-mode WxH@FPS")
        sys.exit(0)

    if args.probe:
        probe_input()
        sys.exit(0)

    if args.list_controllers:
        found = list_controllers()
        if not found:
            print("[-] No controllers detected.")
        for i, name, mapped in found:
            tag = "" if mapped else "   (no GameController mapping - unusable)"
            if name.strip().lower() in OUNCE_SELF_NAMES:
                tag += "   <-- this is the Ounce target itself"
            print(f"  [{i}] {name}{tag}")
        sys.exit(0)

    # One keyboard binding map shared by every slot using the keyboard, held by
    # reference so a remap in the toolbar takes effect immediately everywhere
    # rather than only on slots assigned afterwards.
    live_keymap = load_saved_keymap()

    # slot_sources[slot] = list of (kind, controller_or_None, label)
    slot_sources = {}
    opened_pads = {}          # controller index -> (Controller, name)

    def open_pad_once(idx):
        if idx not in opened_pads:
            c = open_controller(idx)
            if c is None:
                sys.exit(1)
            opened_pads[idx] = (c, pad_name(idx))
        return opened_pads[idx]

    if args.config:
        try:
            cfg = load_config(args.config)
        except Exception as e:
            print(f"[-] Bad config {args.config}:\n      {e}")
            sys.exit(1)
        for slot_s, sc in cfg["slots"].items():
            slot = int(slot_s)
            kb = sc.get("keyboard") or {}
            pad_ref = sc.get("pad")
            pad_over = sc.get("pad_buttons") or {}
            if kb:
                slot_sources.setdefault(slot, []).append(
                    ("keyboard", kb, f"keyboard ({len(kb)} bindings)"))
            if pad_ref is not None:
                idx = resolve_pad(str(pad_ref))
                if idx is None:
                    print(f"[-] slot {slot}: no controller matching '{pad_ref}'")
                    sys.exit(1)
                c, name = open_pad_once(idx)
                merged = dict(DEFAULT_PAD_BUTTONS)
                merged.update({k: v for k, v in pad_over.items()})
                slot_sources.setdefault(slot, []).append(
                    ("pad", (c, merged), f"[{idx}] {name}"
                     + (f" (+{len(pad_over)} rebinds)" if pad_over else "")))
    elif not args.assign and (args.window or os.environ.get("OUNCE_WINDOW") == "1"
                              or os.environ.get("SteamAppId")
                              or os.environ.get("SteamGameId")):
        # With a window there is a toolbar to manage slots from, so do not
        # block on a console prompt. Start with Controller 1 on the keyboard.
        slot_sources[0] = [("keyboard", live_keymap, "keyboard")]

    elif not args.assign and not args.no_interactive and sys.stdin.isatty():
        # Run bare from a terminal: ask which input drives each virtual
        # controller. Skipped when piped/redirected (scripts, Steam) so
        # automation never blocks on a prompt.
        chosen = prompt_slot_assignments()
        if chosen is None:
            sys.exit(1)
        for slot, entries in chosen.items():
            for kind, ref in entries:
                if kind == "keyboard":
                    slot_sources.setdefault(slot, []).append(
                        ("keyboard", live_keymap, "keyboard"))
                else:
                    idx = resolve_pad(ref)
                    if idx is None:
                        print(f"[-] Controller '{ref}' vanished before start.")
                        sys.exit(1)
                    c, name = open_pad_once(idx)
                    slot_sources.setdefault(slot, []).append(
                        ("pad", (c, dict(DEFAULT_PAD_BUTTONS)), f"[{idx}] {name}"))

    elif args.assign:
        for spec in args.assign:
            try:
                slots, (kind, ref) = parse_assignment(spec)
            except ValueError as e:
                print(f"[-] Bad --assign '{spec}': {e}")
                sys.exit(1)
            if kind == "keyboard":
                for slot in slots:
                    slot_sources.setdefault(slot, []).append(
                        ("keyboard", live_keymap, "keyboard"))
                continue
            if ref is None:
                c = open_controller(None)      # auto-pick, skipping our own targets
                if c is None:
                    sys.exit(1)
                idx = next((i for i in range(_pg.joystick.get_count())
                            if _pg.joystick.Joystick(i).get_name().strip().lower()
                            not in OUNCE_SELF_NAMES), 0)
                opened_pads.setdefault(idx, (c, pad_name(idx)))
                name = opened_pads[idx][1]
            else:
                idx = resolve_pad(ref)
                if idx is None:
                    print(f"[-] No controller matching '{ref}' (see --list-controllers).")
                    sys.exit(1)
                c, name = open_pad_once(idx)
            for slot in slots:
                slot_sources.setdefault(slot, []).append(
                    ("pad", (c, dict(DEFAULT_PAD_BUTTONS)), f"[{idx}] {name}"))
    else:
        # Legacy single-slot behaviour: everything drives one slot.
        legacy = []
        if args.input in ("auto", "gamepad"):
            c = open_controller(args.controller)
            if c is None:
                if args.input == "gamepad":
                    print("[-] --input gamepad requested but no usable controller. Exiting.")
                    sys.exit(1)
                print("[+] Falling back to keyboard input.")
            else:
                legacy.append(("pad", (c, dict(DEFAULT_PAD_BUTTONS)), "controller"))
        if args.input != "gamepad":
            legacy.append(("keyboard", live_keymap, "keyboard"))
        slot_sources[args.target] = legacy

    # The enabled set is whatever was assigned - it need not be contiguous, so
    # "only slots 1 and 3" is expressible. This mask is sent to the master in
    # every packet so it knows which targets to drive.
    active_slots = sorted(slot_sources)
    enabled_mask = 0
    for s in active_slots:
        enabled_mask |= (1 << s)
    if not active_slots:
        print("[-] No player slots enabled - nothing to drive. "
              "Use --assign or --config.")
        sys.exit(1)

    print("[+] Player slots (enabled mask 0x%X):" % enabled_mask)
    for s in range(NUM_SLOTS):
        if s in slot_sources:
            labels = ", ".join(lbl for _, _, lbl in slot_sources[s]) or "(nothing)"
            print(f"      slot {s}: {labels}")
        else:
            print(f"      slot {s}: disabled")

    port_name = args.port or find_primary_port()
    if not port_name:
        print("[-] Error: Primary RP2350 Master board serial port not found!")
        sys.exit(1)

    print(f"[+] Auto-detected Primary RP2350 port: {port_name}")
    print(f"[+] Connecting to Primary RP2350 on {port_name} @ 115200 baud...")

    try:
        ser = serial.Serial(port_name, 115200, timeout=0.05, write_timeout=0.05)
    except Exception as e:
        print(f"[-] Error opening serial port {port_name}: {e}")
        sys.exit(1)

    print("[+] Connected! Driving slot(s) %s"
          % ", ".join(str(s) for s in active_slots))

    if args.config:
        # Bindings came from the config file; the slot table printed above
        # already describes them, and printing the built-in map here would
        # actively mislead.
        print("\n[+] Bindings loaded from %s" % args.config)
    else:
        print("\n--- Switch Pro Control Mapping (default keyboard map) ---")
        print("  WASD           : Left Analog Stick (Up, Down, Left, Right)")
        print("  7 8 9 0        : Right Analog Stick (Left, Up, Right, Down)")
        print("  Arrow Keys     : D-Pad (Up, Down, Left, Right)")
        print("  I / J / K / L  : X (top) / Y (left) / B (bottom) / A (right)")
        print("  Y / U / O / P  : ZL / L / R / ZR")
        print("  N / M          : Start (+) / Select (-)")
        print("  Z / X          : L3 / R3 Stick Clicks")
        print("  H / C          : Home / Capture")
        print("  (--dump-config writes this out as an editable file)")
    print("  Close the window (or Ctrl+C) to exit.")
    print("------------------------------------------")
    if opened_pads:
        print("[+] Pads: Guide = Home, Share/Capture = Capture.")

    # Capture card preview. Only meaningful when a window exists, which is the
    # Steam case - the window has to be there for Steam Input anyway, so we may
    # as well put the game on it.
    capture = capture_audio = player = None
    capture_args = None    # how to rebuild the ffmpeg pipe, once there is one
    video_aspect = None
    toolbar = Toolbar()
    # Note this can only ever be the CAPTURE stream. The card's HDMI
    # passthrough is a hardware path from the card to a display and never
    # reaches the PC, so it cannot be drawn here - passthrough is the better
    # picture (4K60/1440p120, lag-free) but it is not available to software.
    want_preview = not (args.no_preview or args.no_capture)

    # Resolved once here rather than read from args at each use: if the separate
    # pipe cannot run, every VlcPreview built below has to keep VLC's own audio,
    # including the ones rebuilt later on a device or mode change.
    split_audio = args.split_audio
    if split_audio and not split_audio_available():
        split_audio = False
        print("[!] --split-audio needs ffmpeg and sounddevice; keeping audio in VLC.")
        if CAPTURE_LATENCY_MS < 100:
            print(f"    {CAPTURE_LATENCY_MS}ms now buffers the sound too and may "
                  f"crackle - raise --capture-latency to 100-200 if it does.")
    toolbar.split_audio = split_audio

    if _window is not None and want_preview:
        vids, auds = list_dshow_devices()
        vname = pick_capture(args.capture, vids)
        aname = pick_capture(args.capture_audio, auds)

        # mpv: the same GPU-direct rendering as VLC, plus a shader stage. This
        # is the only backend that can attack aliasing baked into the signal,
        # because it is the only one that lets a GLSL pass run between decode
        # and display.
        if vname and args.video_backend == "mpv":
            hwnd = None
            try:
                hwnd = _pg.display.get_wm_info().get("window")
            except Exception:
                pass
            found = mpv_exe(args.mpv_path)
            if not found:
                print("[!] mpv not found - install it, put mpv.exe on PATH, or "
                      "pass --mpv-path. Falling back to VLC.")
                args.video_backend = "vlc"
            elif hwnd:
                child = create_video_child(hwnd)
                if child:
                    hwnd = child
                mode = pick_best_mode(list_dshow_modes(vname), args.capture_mode)
                shader = resolve_shader(args.shader)
                if args.shader and not shader:
                    print(f"[!] No shader matching '{args.shader}' "
                          f"(--list-shaders to see what is there)")
                print(f"[+] Capture (mpv, GPU): {vname}")
                print(f"    mpv: {found}")
                if mode:
                    print(f"    mode: {mode[0]}x{mode[1]} @{mode[2]}fps {mode[3]}"
                          f"   (--list-modes for alternatives)")
                print(f"    scaler: {args.scaler}"
                      + ("   (only applies when window size != source)"
                         if mode and mode[:2] == (WINDOW_W, WINDOW_H) else ""))
                print(f"    shader: {os.path.basename(shader) if shader else 'none'}"
                      f"   (toolbar: picture -> Shader)")
                player = MpvPreview(hwnd, vname,
                                    None if split_audio else aname, mode,
                                    exe=found, shader=shader,
                                    scaler=args.scaler, dscale=args.dscale)
                if player.error:
                    print(f"[!] mpv backend failed: {player.error}")
                    player.stop()
                    player = None
                    args.video_backend = "vlc"
                else:
                    toolbar.set_sources(vids, list_dshow_modes(vname), vname, mode)
                    toolbar.mpv_preview = True
                    toolbar.shader = shader
                    toolbar.scaler = args.scaler
                    toolbar.dscale = args.dscale
                    print(f"    supersampling: {args.dscale} kernel, "
                          f"gamma-correct - active whenever the window is "
                          f"smaller than {mode[0]}x{mode[1]}"
                          if mode else
                          f"    supersampling: {args.dscale} kernel")
                    if mode:
                        video_aspect = mode[0] / mode[1]
                        snap_window_to_aspect(video_aspect,
                                              (WINDOW_W, WINDOW_H + TOOLBAR_H))
                    print("    F11 = borderless fullscreen, Esc = leave it")
                    vname = None      # handled; skip the branches below
                    if not split_audio:
                        aname = None

        # Preferred path: VLC draws on the GPU into this very window. Nothing
        # about the video crosses into Python, so 4K60 is limited by the GPU
        # rather than by pipe bandwidth, and VLC plays the audio itself.
        if vname and args.video_backend == "vlc":
            hwnd = None
            try:
                hwnd = _pg.display.get_wm_info().get("window")
            except Exception:
                pass
            if hwnd:
                # VLC renders into a child window so the toolbar strip above it
                # stays ours to draw on.
                child = create_video_child(hwnd)
                if child:
                    hwnd = child
                mode = pick_best_mode(list_dshow_modes(vname), args.capture_mode)
                print(f"[+] Capture (VLC, GPU): {vname}")
                if mode:
                    print(f"    mode: {mode[0]}x{mode[1]} @{mode[2]}fps {mode[3]}"
                          f"   (--list-modes for alternatives)")
                else:
                    print("    mode: card advertised none; letting DirectShow choose")
                if aname and split_audio:
                    print(f"    audio: {aname} -> separate pipe "
                          f"({AUDIO_LATENCY_MS}ms behind; --audio-latency if it "
                          f"crackles), so the {CAPTURE_LATENCY_MS}ms buffer is "
                          f"video only")
                elif aname:
                    print(f"    audio: {aname} -> default Windows output "
                          f"({CAPTURE_LATENCY_MS}ms buffer; raise "
                          f"--capture-latency if it crackles, or --split-audio "
                          f"to take it out of VLC)")
                player = VlcPreview(hwnd, vname,
                                         None if split_audio else aname, mode)
                if player.error:
                    print(f"[!] VLC backend failed: {player.error}")
                    print("    Falling back to the ffmpeg pipe (lower resolution).")
                    player = None
                else:
                    # Keep the window at the source's aspect so the capture
                    # fills it exactly - no letterbox bars, no stretching.
                    toolbar.set_sources(vids, list_dshow_modes(vname), vname, mode)
                    if mode and mode[0] and mode[1]:
                        video_aspect = mode[0] / mode[1]
                        if args.fullscreen:
                            toggle_borderless_fullscreen(True)
                        # Size the window so the video AREA is the source
                        # aspect, with the toolbar strip added on top of that.
                        snap_window_to_aspect(video_aspect,
                                              0 if is_fullscreen() else TOOLBAR_H)
                        layout_video_child(show_toolbar=not is_fullscreen())
                        player.fit(video_child_size(not is_fullscreen()))
                        print("    F11 = borderless fullscreen, Esc = leave it")
                        print("    toolbar: pick input device and capture mode")
                    vname = None      # handled; skip the ffmpeg path below
                    # With --split-audio, aname is deliberately left set so the
                    # CaptureAudio block below picks it up - VLC was handed no
                    # audio device, so nothing else would play the sound.
                    if not split_audio:
                        aname = None

        if vname:
            if args.capture_size == "auto":
                # With the upscaler on, the pipe carries the window size so the
                # picture is resampled once, by ffmpeg, at the size it is
                # actually displayed at. The old fixed 960x540 meant a shrink
                # in ffmpeg and a stretch back in pygame - two lossy resamples,
                # neither at the display size, which is most of why this path
                # looked soft.
                pw, ph = ((WINDOW_W, WINDOW_H) if UPSCALE != "off"
                          else (CAPTURE_W, CAPTURE_H))
            else:
                try:
                    pw, ph = (int(x) for x in args.capture_size.lower().split("x"))
                except Exception:
                    print(f"[-] Bad --capture-size '{args.capture_size}', using default")
                    pw, ph = CAPTURE_W, CAPTURE_H
            print(f"[+] Capture video: {vname} -> {pw}x{ph} @{args.capture_fps}fps"
                  + (f"  (upscale {UPSCALE}, render {UPSCALE_RENDER_H or 'native'}, "
                     f"sharpen {UPSCALE_SHARPEN})" if UPSCALE != "off" else ""))
            # Kept so a toolbar change can rebuild the pipe with exactly the
            # same inputs. The filter graph is fixed when ffmpeg starts, so
            # every upscaler setting is applied by restarting it.
            capture_args = (vname, pw, ph, args.capture_fps,
                            args.capture_input_size, args.capture_input_fps)
            capture = CapturePreview(*capture_args)
            if capture.error:
                print(f"[!] Capture video failed: {capture.error}")
                capture = None
            else:
                toolbar.software_preview = True
        elif args.capture:
            print(f"[-] No capture device matching '{args.capture}' "
                  f"(try --list-capture)")

        # Needed on the ffmpeg path, and on the VLC path under --split-audio,
        # where VLC was given no audio device on purpose.
        if aname:
            print(f"[+] Capture audio: {aname} -> default Windows output")
            capture_audio = CaptureAudio(aname)
            if capture_audio.error:
                print(f"[!] Capture audio failed: {capture_audio.error}")
                capture_audio = None

    def _slot_state():
        """Current slot assignment in the shape the toolbar wants.

        A slot can hold a pad and the keyboard at once, so the pad is the
        primary source whenever there is one, and the keyboard is reported
        separately as an addition rather than as the assignment.
        """
        out, kb = {}, {}
        for s in range(NUM_SLOTS):
            entries = slot_sources.get(s) or []
            pad = next((e for e in entries if e[0] == "pad"), None)
            out[s] = None if pad is None else next(
                (i for i, (c, _n) in opened_pads.items() if c is pad[1][0]), None)
            kinds = {e[0] for e in entries}
            kb[s] = (KB_FULL if "keyboard" in kinds else
                     KB_AUX if "keyboard_aux" in kinds else KB_OFF)
        return out, kb

    # Seed the toolbar with the real pad list and current assignment, so P1-P4
    # show the truth from the first frame rather than after the first click.
    if _window is not None:
        toolbar.set_inputs(list_real_pads(), *_slot_state())
        toolbar.keymap = live_keymap      # edited in place by the remap screen
    recorder = None                       # InputRecorder while recording

    total_sent = 0
    last_paint = 0.0
    refit_until = 0.0      # keep re-fitting VLC until this time (see _refit)
    menu_was_open = False  # tracks video-child visibility vs the dropdown
    def _refit():
        # Snap back to the source aspect, then tell VLC to fill the
        # new client area, so a resize leaves neither bars nor a
        # squished picture.
        #
        # VLC applies a geometry change asynchronously, so a single
        # fit() right after the resize lands before VLC has caught
        # up and briefly shows bars. Keep re-applying it for a short
        # while so the transition stays clean.
        nonlocal refit_until
        snap_window_to_aspect(video_aspect,
                              0 if is_fullscreen() else TOOLBAR_H)
        layout_video_child(show_toolbar=not is_fullscreen())
        if player:
            player.fit(video_child_size(not is_fullscreen()))
        refit_until = time.monotonic() + 0.6

    def _swap_player(make_player):
        """Replace the VLC player, pumping messages during teardown.

        The pump is load-bearing: stop() cannot finish unless the
        window's message loop keeps running (see stop_async)."""
        nonlocal player
        if player is not None:
            done = player.stop_async()
            deadline = time.monotonic() + 4.0
            while not done.is_set() and time.monotonic() < deadline:
                pump_window(vlc_active=True)
                time.sleep(0.01)
        player = make_player()
        if player.error:
            print(f"[!] {player.error}")
        _refit()

    def _set_slot(slot, entries):
        """Install a player's sources and republish what the master polls.

        A slot with no sources left is removed entirely rather than kept as an
        empty list - enabled_mask is derived from which slots exist, and an
        empty one would have the master polling a player nothing drives.
        """
        nonlocal enabled_mask, active_slots
        if entries:
            slot_sources[slot] = entries
        else:
            slot_sources.pop(slot, None)
        active_slots = sorted(slot_sources)
        enabled_mask = 0
        for s in active_slots:
            enabled_mask |= (1 << s)
        toolbar.set_inputs(toolbar.pads, *_slot_state())

    def _on_click(pos):
        """Toolbar click: switch input device or capture mode.

        Both require tearing down and restarting VLC - DirectShow
        negotiates the device and mode when the stream opens, so
        they cannot be changed on a live one."""
        # One declaration for the whole handler: Python wants nonlocal before
        # any use of the name, and both the audio-buffer and record branches
        # rebuild capture_audio.
        nonlocal player, video_aspect, refit_until, recorder, capture_audio
        nonlocal capture, capture_args
        # Fullscreen hides the toolbar, so a click there is never aimed at it.
        # This used to bail on the ffmpeg backend too (player is None),
        # which made the whole menu inert exactly where the upscaler lives; the
        # VLC-only handlers below check for their player instead.
        if is_fullscreen():
            return
        if player is None and not toolbar.software_preview:
            return
        # Re-enumerate on every toolbar click, so a controller connected after
        # the client started still shows up. SDL does notice the device (the
        # event pump raises JOYDEVICEADDED), but the menu was built from a list
        # taken once at startup - which is why a Parsec pad, or anything else
        # plugged in later, appeared to be unsupported when it was simply not
        # being looked for again.
        toolbar.set_inputs(list_real_pads(), *_slot_state())
        # Ask the player whether it is actually supersampling before drawing the
        # menu that reports it. One bounded IPC call per click, so the answer
        # tracks the window size instead of whatever was true at startup.
        if isinstance(player, MpvPreview):
            toolbar.supersampling = player.supersampling(
                video_child_size(not is_fullscreen()))
        picked = toolbar.click(pos)
        if not picked:
            return
        kind, value = picked

        if kind == "keymap":
            # "reset all" put the defaults back; persist that too, or the old
            # remaps would return on the next launch.
            save_keymap(live_keymap)
            return

        if kind == "record":
            if isinstance(player, MpvPreview):
                # mpv toggles stream-record on a live player, so unlike the VLC
                # path this needs no restart - which also means it cannot
                # truncate the take, and audio is whatever mpv is already
                # playing rather than something handed back for the duration.
                if value:
                    if toolbar.mode and toolbar.mode[3].lower() not in ("mjpeg",
                                                                        "mjpg"):
                        w, h, fps, pixfmt = toolbar.mode
                        print(f"[!] {pixfmt} is uncompressed - recording at "
                              f"roughly {w * h * 1.5 * fps / 1e6:.0f} MB/s.")
                    d = new_recording_dir()
                    # Matroska, not AVI: mpv writes the stream in whatever codec
                    # it arrives in, and mkv carries raw and MJPEG alike without
                    # the size limits AVI brings.
                    if player.set_recording(os.path.join(d, "capture.mkv")):
                        recorder = InputRecorder(d)
                        toolbar.recording = True
                        print(f"[+] Recording to {d}")
                    else:
                        print("[-] mpv refused to start recording.")
                else:
                    player.set_recording(None)
                    toolbar.recording = False
                    if recorder is not None:
                        rows, length = recorder.rows, recorder.elapsed()
                        recorder.close()
                        where = finish_recording_dir(recorder.dir, length)
                        recorder = None
                        print(f"[+] Recording saved: {where}  "
                              f"({rows} input rows)")
                return
            if player is None:
                # VLC's sout is what writes the file; the ffmpeg preview pipe
                # has no recorder behind it.
                print("[-] Recording needs the VLC or mpv backend.")
                return
            aud = pick_capture(args.capture_audio, list_dshow_devices()[1])
            # VLC is what writes capture.avi, so it needs the audio device back
            # for the length of a recording or the file would be silent. The
            # separate pipe stops for that time, otherwise the sound would play
            # twice. Latency rises while recording and drops again on stop.
            if split_audio and aud:
                if value and capture_audio is not None:
                    capture_audio.stop()
                    capture_audio = None
            if value:                                  # start
                # Frames are written in the codec they arrive in, so a raw
                # capture mode records raw: 1080p60 NV12 is ~180MB/s, which
                # fills a disk in minutes. Worth saying before it happens
                # rather than after.
                if toolbar.mode and toolbar.mode[3].lower() not in ("mjpeg", "mjpg"):
                    w, h, fps, pixfmt = toolbar.mode
                    print(f"[!] {pixfmt} is uncompressed - recording at roughly "
                          f"{w * h * 1.5 * fps / 1e6:.0f} MB/s. "
                          f"Switch to an MJPEG mode for a smaller file.")
                d = new_recording_dir()
                recorder = InputRecorder(d)
                toolbar.recording = True
                _swap_player(lambda: VlcPreview(
                    _video_child, toolbar.device, aud, toolbar.mode,
                    record_path=os.path.join(d, "capture.avi")))
                if player.error:
                    toolbar.recording = False
                    recorder.close(); recorder = None
                    print("[-] Recording failed to start.")
                else:
                    print(f"[+] Recording to {d}")
            else:                                      # stop
                toolbar.recording = False
                # Tear the recording pipeline down first: the file is only
                # finalised when VLC closes it, so the log must not be cut
                # short before the video it is timed against.
                _swap_player(lambda: VlcPreview(
                    _video_child, toolbar.device,
                    None if split_audio else aud, toolbar.mode))
                if split_audio and aud:
                    # Hand the sound back to the separate pipe.
                    capture_audio = CaptureAudio(aud)
                    if capture_audio.error:
                        print(f"[!] Capture audio failed: {capture_audio.error}")
                        capture_audio = None
                if recorder is not None:
                    rows, length = recorder.rows, recorder.elapsed()
                    recorder.close()
                    where = finish_recording_dir(recorder.dir, length)
                    recorder = None
                    print(f"[+] Recording saved: {where}  "
                          f"({rows} input rows)")
            return

        if kind == "alat":
            set_audio_latency(value)
            # The cushion is primed once at start, so it only changes by
            # restarting the pipe. Cheap - a few hundred ms of silence - and it
            # does not touch the video, which is the whole point of the split.
            if capture_audio is not None:
                dev = pick_capture(args.capture_audio, list_dshow_devices()[1])
                capture_audio.stop()
                capture_audio = CaptureAudio(dev) if dev else None
                if capture_audio is not None and capture_audio.error:
                    print(f"[!] Capture audio failed: {capture_audio.error}")
                    capture_audio = None
            print(f"[+] Audio buffer -> {AUDIO_LATENCY_MS}ms")
            return

        if kind == "dscale":
            if not isinstance(player, MpvPreview):
                print("[-] Supersampling is an mpv setting "
                      "(--video-backend mpv). On the ffmpeg backend it is "
                      "'Upscaler: aa' instead.")
                return
            if player.set_dscale(value):
                toolbar.dscale = value
                active, why = player.supersampling(
                    video_child_size(not is_fullscreen()))
                toolbar.supersampling = (active, why)
                print(f"[+] Supersample kernel -> {value}"
                      + (f"   (active, {why})" if active else
                         f"   (idle: {why})" if active is False else ""))
            else:
                print(f"[!] mpv refused dscale {value}")
            return

        if kind in ("shader", "scaler"):
            # The one settings pair that needs no restart at all: mpv swaps
            # shaders and scalers on a live stream, so this is safe mid-take
            # and costs nothing but a frame.
            if not isinstance(player, MpvPreview):
                print("[-] Shaders need the mpv backend (--video-backend mpv).")
                return
            if kind == "shader":
                ok = player.set_shader(value)
                toolbar.shader = value if ok else toolbar.shader
                name = os.path.basename(value)[:-5] if value else "none"
                print(f"[+] Shader -> {name}" if ok
                      else f"[!] mpv refused shader {name}")
            else:
                ok = player.set_scaler(value)
                toolbar.scaler = value if ok else toolbar.scaler
                print(f"[+] Scaler -> {value}" if ok
                      else f"[!] mpv refused scaler {value}")
            return

        if kind in ("up", "rh", "sharp"):
            # ffmpeg builds its filter graph once, when it starts, so every one
            # of these is applied by restarting the pipe rather than by poking
            # a live process. That costs a brief black frame and nothing else -
            # the input loop runs on its own thread and never waits on capture.
            if kind == "up":
                set_upscale(value)
            elif kind == "rh":
                set_render_height(value)
            else:
                set_sharpen(value)
            if capture is not None and capture_args:
                dev, _pw, _ph, cfps, isize, ifps = capture_args
                # The pipe carries the window size while the upscaler is on, so
                # ffmpeg resamples once at the size actually displayed; off, it
                # goes back to the cheap fixed size.
                if UPSCALE != "off":
                    _pw, _ph = WINDOW_W, WINDOW_H
                capture.stop()
                capture = CapturePreview(dev, _pw, _ph, cfps, isize, ifps)
                if capture.error:
                    print(f"[!] Capture video failed: {capture.error}")
                    capture = None
                    toolbar.software_preview = False
            print(f"[+] Upscaler -> {UPSCALE}"
                  + (f", render {UPSCALE_RENDER_H or 'native'}, "
                     f"sharpen {UPSCALE_SHARPEN:.1f}" if UPSCALE != "off" else ""))
            return

        if kind == "vlat":
            if player is None:
                print("[-] The video buffer is a VLC setting; this is the "
                      "ffmpeg backend.")
                return
            if toolbar.recording:
                print("[-] Stop recording before changing the video buffer.")
                return
            set_capture_latency(value)
            # live-caching is fixed when the media is created, so this needs the
            # same stream restart a device or mode change needs.
            aud = pick_capture(args.capture_audio, list_dshow_devices()[1])
            _swap_player(lambda: VlcPreview(_video_child, toolbar.device,
                                         None if split_audio else aud,
                                         toolbar.mode))
            print(f"[+] Video buffer -> {CAPTURE_LATENCY_MS}ms")
            return

        if toolbar.recording and kind in ("device", "mode"):
            # Restarting the stream is how the device and mode settings
            # are applied, and that same restart truncates the file being
            # written. Refused rather than silently losing the take.
            print("[-] Stop recording before changing the capture settings.")
            return

        if kind == "slotkb":
            # How much keyboard rides along with whatever pad the slot has.
            # merge_inputs combines them, so a pad keeps working and the
            # keyboard only supplies what the chosen mode allows.
            slot, mode = value
            entries = [e for e in (slot_sources.get(slot) or [])
                       if e[0] not in ("keyboard", "keyboard_aux")]
            if mode == KB_FULL:
                entries.append(("keyboard", live_keymap, "keyboard"))
            elif mode == KB_AUX:
                entries.append(("keyboard_aux", live_keymap,
                                "keyboard (Home/Capture)"))
            _set_slot(slot, entries)
            print(f"[+] Controller {slot + 1}: keyboard {KB_MODE_NAMES[mode]}")
            return

        if kind == "slot":
            slot, src = value
            # The keyboard mode belongs to the player, not to the particular
            # pad, so swapping pads keeps it rather than silently dropping it
            # and losing Home/Capture again.
            entries = [e for e in (slot_sources.get(slot) or [])
                       if e[0] != "pad"]
            if src is not None:
                c, name = open_pad_once(src)
                entries.insert(0, ("pad", (c, dict(DEFAULT_PAD_BUTTONS)),
                                   f"[{src}] {name}"))
            _set_slot(slot, entries)
            print(f"[+] Controller {slot + 1} -> "
                  f"{'no controller' if src is None else src}")
            return

        new_dev = value if kind == "device" else toolbar.device
        new_modes = list_dshow_modes(new_dev) if kind == "device" else None
        new_mode = (pick_best_mode(new_modes) if kind == "device" else value)

        print(f"[+] Switching {kind} -> "
              f"{value if kind == 'device' else mode_label(value)}")
        if player is None:
            # ffmpeg backend: the same change, applied by restarting the pipe.
            # DirectShow negotiates device and mode when the stream opens on
            # either backend, so neither can be changed on a live one.
            if capture is not None and capture_args:
                _, _pw, _ph, cfps, _isize, ifps = capture_args
                if UPSCALE != "off":
                    _pw, _ph = WINDOW_W, WINDOW_H
                isize = (f"{new_mode[0]}x{new_mode[1]}" if new_mode else None)
                capture.stop()
                capture = CapturePreview(new_dev, _pw, _ph, cfps, isize, ifps)
                capture_args = (new_dev, _pw, _ph, cfps, isize, ifps)
                if capture.error:
                    print(f"[!] Capture video failed: {capture.error}")
                    capture = None
                    toolbar.software_preview = False
            toolbar.set_sources(toolbar.devices,
                                new_modes if new_modes is not None
                                else (toolbar.modes_comp + toolbar.modes_raw),
                                new_dev, new_mode)
            return
        aud = pick_capture(args.capture_audio, list_dshow_devices()[1])
        # Under --split-audio the separate pipe is already playing this card's
        # sound and keeps running across a device or mode change, so the player
        # must not also open it.
        if isinstance(player, MpvPreview):
            # Rebuilt rather than retuned, for the same reason as VLC: the
            # device and mode are negotiated when the stream opens. The shader
            # and scaler are carried across so a mode change does not silently
            # drop them.
            keep = (toolbar.shader, toolbar.scaler, toolbar.dscale)
            _swap_player(lambda: MpvPreview(
                _video_child, new_dev, None if split_audio else aud, new_mode,
                exe=mpv_exe(args.mpv_path), shader=keep[0],
                scaler=keep[1], dscale=keep[2]))
        else:
            _swap_player(lambda: VlcPreview(_video_child, new_dev,
                                            None if split_audio else aud,
                                            new_mode))
        toolbar.set_sources(toolbar.devices,
                            new_modes if new_modes is not None
                            else (toolbar.modes_comp + toolbar.modes_raw),
                            new_dev, new_mode)
        if new_mode:
            video_aspect = new_mode[0] / new_mode[1]
        _refit()

    def _on_key(key):
        """Feed keypresses to the remap screen while it is waiting.
        Returns True if the key was consumed."""
        if toolbar.capture_action:
            # Read it before binding - bind_key clears capture_action, so
            # reporting it afterwards always said "input".
            action = toolbar.capture_action
            if toolbar.bind_key(_pg, key):
                print("[+] bound %s -> %s" %
                      (ACTION_LABEL.get(action, action), _pg.key.name(key)))
                save_keymap(live_keymap)
            return True
        return False



    # 1 kHz, paced against a deadline rather than by sleeping a flat period.
    # time.sleep() is a floor, not a period: measured here a 2ms request returns
    # after 2.31ms mean / 2.50ms p50, so a flat sleep ran the loop at ~426Hz and
    # the overshoot was pure added latency. Deadline pacing measures 999.9Hz at
    # this period, p99 1.40ms.
    #
    # 1ms rather than 2ms because that is the master's own frame period: sending
    # faster than the master polls only ages packets it will overwrite, sending
    # slower means it re-sends a stale one. Four slots at 1kHz is 36kB/s against
    # the 64kB/s the master can drain (64 bytes per 1ms frame), and building all
    # four packets costs 0.04ms of the 1ms.
    SEND_PERIOD = 0.001
    next_send = time.perf_counter()

    try:
        while True:
            # Esc deliberately does NOT quit. It is a normal game key and it
            # also leaves fullscreen and cancels a pending rebind, so quitting
            # on it meant an in-game press could close the bridge mid-session.
            # Closing is the window's X, or Ctrl+C from a console.
            if args.max_packets > 0 and total_sent >= args.max_packets:
                print("\n[+] Reached maximum packet limit of %d. Exiting." % args.max_packets)
                break

            # 1. Build and send one packet per enabled slot. Each slot merges
            # all of its own sources, so a slot can be driven by a keyboard and
            # a pad at once, and different slots by different devices.
            # While a key is being captured for remapping, do not also send that
            # keypress to the controllers - you would be pressing buttons in
            # game while rebinding them.
            if toolbar.capture_action:
                time.sleep(0.005)
                if _window is not None and (time.monotonic() - last_paint) >= 0.033:
                    last_paint = time.monotonic()
                    if not pump_window(vlc_active=(player is not None),
                                       on_click=_on_click,
                                       on_key=_on_key):
                        break
                    _window.fill((16, 16, 20))
                    toolbar.draw(_window)
                    _pg.display.flip()
                continue

            for slot in active_slots:
                states = []
                for kind, obj, _ in slot_sources[slot]:
                    try:
                        if kind == "keyboard":
                            states.append(read_keyboard_mapped(obj))
                        elif kind == "keyboard_aux":
                            # Filtered per read, off the live map, so remapping
                            # Home or Capture still takes effect immediately -
                            # a snapshot taken when the mode was chosen would
                            # have frozen those two keys.
                            states.append(read_keyboard_mapped(
                                {a: k for a, k in obj.items()
                                 if a in KB_AUX_ACTIONS}))
                        else:
                            pad_obj, pad_binds = obj
                            states.append(read_controller(
                                pad_obj, args.deadzone, args.trigger_threshold,
                                pad_binds))
                    except Exception as e:
                        print(f"[-] slot {slot} source read failed ({e}); skipping it.")
                buttons, lx, ly, rx, ry, aux = merge_inputs(*states)
                try:
                    ser.write(make_serial_packet(slot, buttons, lx, ly, rx, ry,
                                                 aux, enabled_mask))
                    total_sent += 1
                except Exception:
                    time.sleep(0.01)
                    break
                if recorder is not None:
                    # Logged after the write, so the file only contains what
                    # actually went to the master.
                    recorder.log(slot, buttons, lx, ly, rx, ry, aux)

            # (Packets for every enabled slot were already sent above. No
            # flush() anywhere: pyserial's Windows flush() is an unbounded
            # busy-wait on the OS output queue, with no timeout, which adds
            # latency and jitter and can block outright if the device stalls.)

            # Repaint the window at ~30fps. Deliberately decoupled from the
            # input loop above, which runs at 500Hz: painting is far more
            # expensive than building a packet and must never pace it.
            if _window is not None and (time.monotonic() - last_paint) >= 0.033:
                last_paint = time.monotonic()
                if not pump_window(vlc_active=(player is not None),
                                   on_resize=(_refit if player else None),
                                   on_click=_on_click, on_key=_on_key):
                    print("\n[+] Window closed - exiting.")
                    break
                if player is not None:
                    # VLC owns the child window's pixels, but the toolbar strip
                    # above it is still ours. Hidden in fullscreen, where the
                    # video child covers the whole window.
                    if time.monotonic() < refit_until:
                        player.fit(video_child_size(not is_fullscreen()))
                    if not is_fullscreen():
                        menu_open = toolbar.open_menu is not None
                        if menu_open != menu_was_open:
                            # The video child would cover the dropdown, so it
                            # steps aside while a menu is up.
                            show_video_child(not menu_open)
                            menu_was_open = menu_open
                            if not menu_open:
                                _refit()
                        if menu_open:
                            _window.fill((16, 16, 20))
                        toolbar.draw(_window)
                        _pg.display.update(_pg.Rect(0, 0, _window.get_width(),
                                                    _window.get_height()
                                                    if menu_open else TOOLBAR_H))
                elif capture is not None and capture.blit_into(_window):
                    _pg.display.flip()
                    if capture.error:
                        print(f"[!] Capture stopped: {capture.error}")
                        capture = None
                else:
                    _draw_status(
                        "waiting for capture signal..." if capture else
                        f"driving {len(active_slots)} controller(s)",
                        extra=[
                            f"slot {s}: " + ", ".join(l for _, _, l in slot_sources[s])
                            for s in active_slots
                        ] + [f"packets sent: {total_sent}"])

            # 2. Drain CDC telemetry and show it
            try:
                while ser.in_waiting > 0:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    print(line)
            except Exception:
                pass

            # Sleep to the next deadline. This was a flat 20ms (50 Hz), which by
            # itself put up to 20ms of latency on every input - far more than
            # the whole SPI path, which delivers a packet every 1ms. The master
            # polls at 1kHz, so feeding it faster than 50 Hz is what actually
            # makes the controller feel responsive.
            next_send += SEND_PERIOD
            slack = next_send - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            elif slack < -SEND_PERIOD:
                # Fallen a whole period behind - a repaint, a mode switch or the
                # OS descheduling us. Give up the lost time rather than trying to
                # win it back: catching up means a burst of packets at whatever
                # rate the CPU allows, which is exactly the free-running hammer
                # the master's own pacing guards against.
                next_send = time.perf_counter()

    except KeyboardInterrupt:
        print("\n[+] Interrupted.")
    except Exception as e:
        print(f"\n[+] Unexpected error caught: {e}")
    finally:
        # Stop the players before finalising, so VLC has closed the video file
        # by the time the folder is renamed. Quitting mid-recording still
        # leaves a complete, correctly named take rather than a stray folder.
        for obj in (capture, capture_audio, player):
            if obj is not None:
                obj.stop()
        if recorder is not None:
            rows, length = recorder.rows, recorder.elapsed()
            recorder.close()
            print(f"[+] Recording saved: {finish_recording_dir(recorder.dir, length)}"
                  f"  ({rows} input rows)")
        try:
            ser.close()
        except Exception:
            pass

if __name__ == '__main__':
    main()
