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

VK_ESC = 0x1B # Exit Bridge

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
        pads = [(i, _pg.joystick.Joystick(i).get_name())
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
        print(f"   [{i}] {jj.get_name()}  axes={jj.get_numaxes()} "
              f"btn={jj.get_numbuttons()} hat={jj.get_numhats()} "
              f"mapped={_sdl_controller.is_controller(i)}{tag}")

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


def list_real_pads():
    """Controllers that are not our own emulated targets.

    The Ounce slaves enumerate as Switch Pro Controllers, so they show up in
    the same list as real hardware; offering them as inputs would just feed our
    own output back in."""
    if not gamepad_available():
        return []
    out = []
    for i in range(_pg.joystick.get_count()):
        name = _pg.joystick.Joystick(i).get_name()
        if name.strip().lower() in OUNCE_SELF_NAMES:
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
        if ref_l in _pg.joystick.Joystick(i).get_name().lower():
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


def pick_best_mode(modes, want=None):
    """Choose a capture mode. `want` is 'WxH', 'WxH@FPS', or None for best.

    With no preference: highest resolution first, then the highest frame rate
    available at that resolution. Resolution is ranked first deliberately -
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
        self.hdr = False
        self.pads = []               # [(idx, name)] real controllers
        self.slots = {}              # slot -> 'keyboard' | pad index | None
        self.open_menu = None        # None | 'device' | 'mode' | 'hdr' | 'slot0'.. | 'keys'
        self._hit = []               # [(rect, kind, value)] rebuilt each draw
        self.keymap = None           # live dict of action -> key name
        self.capture_action = None   # action awaiting a keypress, if any
        self.recording = False       # drives the record button's appearance

    def set_sources(self, devices, modes, device, mode):
        self.devices = devices
        self.modes_raw, self.modes_comp = group_modes(modes)
        self.device = device
        self.mode = mode

    def set_inputs(self, pads, slots):
        self.pads = pads
        self.slots = dict(slots)

    def _slot_label(self, slot, short=True):
        v = self.slots.get(slot)
        if v is None:
            return "off" if short else "Disabled"
        if v == "keyboard":
            return "kbd" if short else "Keyboard"
        for i, n in self.pads:
            if i == v:
                return n.split("(")[0].strip()[:12 if short else 40]
        return f"pad{v}"

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
        on = [f"P{s + 1}" for s in range(NUM_SLOTS) if self.slots.get(s) is not None]
        bits = [mode_label(self.mode) if self.mode else "no mode",
                "HDR on" if self.hdr else "HDR off",
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
                     ("go", f"HDR   :  {'on' if self.hdr else 'off'}", "hdr"),
                     (None, "-- controllers --", None)]
            # Spell out what each controller is currently driven by, rather
            # than an abbreviation - this is the screen people come here to read.
            items += [("go", f"Controller {s + 1} :  {self._slot_label(s, short=False)}",
                       f"slot{s}") for s in range(NUM_SLOTS)]
            items += [(None, "-- keyboard --", None),
                      ("go", "Remap keyboard controls...", "keys")]
        elif self.open_menu == "device":
            items = [("device", d, d) for d in self.devices]
        elif self.open_menu == "hdr":
            items = [("hdr", "HDR on  (pass through, no tone mapping)", True),
                     ("hdr", "HDR off (tone map to SDR)", False)]
        elif self.open_menu == "keys":
            self._draw_keymap(surface, f)
            return
        elif self.open_menu and self.open_menu.startswith("slot"):
            slot = int(self.open_menu[4:])
            items = [(None, f"-- Controller {slot + 1}  (now: "
                            f"{self._slot_label(slot, short=False)}) --", None),
                     ("slot", "Keyboard", (slot, "keyboard"))]
            items += [("slot", name, (slot, idx)) for idx, name in self.pads]
            items.append(("slot", "Disabled", (slot, None)))
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
        wmenu = max([f.size(t)[0] for _, t, _ in items] or [200]) + 28
        hmenu = rowh * len(items) + pad * 2
        mx, my = menu_x, TOOLBAR_H
        mx = max(4, min(mx, surface.get_width() - wmenu - 4))
        hmenu = min(hmenu, surface.get_height() - my - 10)

        _pg.draw.rect(surface, (24, 24, 30), (mx, my, wmenu, hmenu))
        _pg.draw.rect(surface, (70, 70, 84), (mx, my, wmenu, hmenu), 1)

        y = my + pad
        for kind, text, value in items:
            if y + rowh > my + hmenu:
                break
            if kind is None:
                surface.blit(f.render(text, True, (130, 130, 150)), (mx + 8, y + 3))
            else:
                if kind == "device":
                    sel = value == self.device
                elif kind == "mode":
                    sel = value == self.mode
                elif kind == "hdr":
                    sel = value == self.hdr
                elif kind == "slot":
                    sel = self.slots.get(value[0]) == value[1]
                else:
                    sel = False          # 'go' rows are navigation, never selected
                rect = _pg.Rect(mx + 2, y, wmenu - 4, rowh)
                if sel:
                    _pg.draw.rect(surface, (48, 74, 58), rect, border_radius=3)
                surface.blit(f.render(text, True, (235, 235, 245)), (mx + 8, y + 3))
                self._hit.append((rect, kind, value))
            y += rowh

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

    # One libvlc Instance per HDR setting, kept for the life of the process.
    # Releasing an Instance and building a new one on every device/mode change
    # deadlocks libvlc, which is what made switching capture modes hang. Only
    # the MediaPlayer is recreated per change.
    _instances = {}

    @classmethod
    def _instance(cls, vlc, hdr):
        inst = cls._instances.get(bool(hdr))
        if inst is None:
            args = [
                "--no-video-title-show",
                "--quiet",
                "--network-caching=0",
                "--live-caching=0",
                "--file-caching=0",
                # HDR handling. An HDR10 capture shown untouched on an SDR
                # display looks washed out, so "off" tone maps it down with
                # Hable (VLC's recommended filmic curve); "on" uses a linear
                # peak-to-peak stretch to pass the range through for a display
                # that can actually show it.
                "--tone-mapping=5" if hdr else "--tone-mapping=3",
            ]
            inst = vlc.Instance(args)
            cls._instances[bool(hdr)] = inst
        return inst

    def __init__(self, hwnd, video_dev, audio_dev=None, mode=None, hdr=False,
                 record_path=None):
        self.error = None
        self._player = None
        self._inst = None
        self.record_path = record_path
        try:
            import vlc
        except Exception:
            self.error = "python-vlc not installed (pip install python-vlc)"
            return
        try:
            # --no-xlib is harmless on Windows; the rest keeps latency down.
            self._inst = self._instance(vlc, hdr)
            self._player = self._inst.media_player_new()
            # The mode MUST be requested explicitly. DirectShow otherwise hands
            # over the first advertised format, which on this card is 640x480 -
            # that is why the picture looked low-res regardless of the source.
            mrl = "dshow://"
            opts = [f":dshow-vdev={video_dev}",
                    f":dshow-adev={audio_dev or 'none'}",
                    ":live-caching=0"]
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
        """Fill the window completely, with no letterbox or pillarbox bars.

        Leaving the aspect ratio unset makes VLC letterbox to the source's own
        aspect, which leaves bars whenever the window is even slightly off that
        shape (and window chrome makes that the normal case). Telling VLC the
        display aspect IS the window's aspect makes it fill edge to edge. The
        window is separately snapped to the source aspect, so this fills
        without visibly distorting anything."""
        try:
            self._player.video_set_scale(0.0)          # 0 = fit to window
            if window_size and window_size[1]:
                w, h = int(window_size[0]), int(window_size[1])
                self._player.video_set_aspect_ratio(f"{w}:{h}")
            else:
                self._player.video_set_aspect_ratio(None)
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
        cmd += ["-i", f"video={device_name}",
                "-vf", f"scale={w}:{h}",
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
        """Draw the newest frame scaled to the window. True if one was drawn."""
        with self._lock:
            frame = self._frame
        if frame is None:
            return False
        try:
            surf = _pg.surfarray.make_surface(frame)
            if surf.get_size() != surface.get_size():
                surf = _pg.transform.smoothscale(surf, surface.get_size())
            surface.blit(surf, (0, 0))
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
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL,
                                          creationflags=_NO_WINDOW)
            self._stream = sd.RawOutputStream(samplerate=rate, channels=channels,
                                              dtype="int16", blocksize=1024)
            self._stream.start()
        except Exception as e:
            self.error = f"audio start failed ({e})"
            self.stop()
            return
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        chunk = 1024 * 2 * 2          # frames * channels * bytes per sample
        while not self._stop.is_set():
            try:
                data = self._proc.stdout.read(chunk)
                if not data:
                    break
                self._stream.write(data)
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
    global _pg, _sdl_controller
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
        out.append((i, j.get_name(), _sdl_controller.is_controller(i)))
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
    print(f"[+] Using controller {index}: {_pg.joystick.Joystick(index).get_name()}")
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
    parser.add_argument("--video-backend", choices=["vlc", "ffmpeg"], default="vlc",
                        help="How the capture card is drawn. 'vlc' renders on the GPU "
                             "straight into the window (handles 4K60, plays its own "
                             "audio). 'ffmpeg' pipes raw frames through Python, which "
                             "caps out near 1080p. Default vlc, falling back to ffmpeg.")
    parser.add_argument("--capture-size", default=f"{CAPTURE_W}x{CAPTURE_H}",
                        metavar="WxH",
                        help="Preview size. Raw frames cross a pipe, so this sets the "
                             "bandwidth: 960x540 is ~1.5MB/frame. Default 960x540.")
    parser.add_argument("--capture-fps", type=int, default=30, metavar="N",
                        help="Preview frame rate cap. Default 30; 60 doubles pipe "
                             "bandwidth for little visible gain.")
    parser.add_argument("--capture-mode", default=None, metavar="WxH[@FPS]",
                        help="Capture mode to request, e.g. 3840x2160@30 or 1920x1080@120. "
                             "Default: the highest pixels-per-second the card offers. "
                             "DirectShow gives you 640x480 unless a mode is requested.")
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
    parser.add_argument("--hdr", action="store_true",
                        help="Start with HDR passthrough instead of tone mapping to SDR. "
                             "Toggleable from the toolbar.")
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
            opened_pads[idx] = (c, _pg.joystick.Joystick(idx).get_name())
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
                opened_pads.setdefault(idx, (c, _pg.joystick.Joystick(idx).get_name()))
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
    print("  ESC            : Exit Bridge")
    print("------------------------------------------")
    if opened_pads:
        print("[+] Pads: Guide = Home, Share/Capture = Capture.")

    # Capture card preview. Only meaningful when a window exists, which is the
    # Steam case - the window has to be there for Steam Input anyway, so we may
    # as well put the game on it.
    capture = capture_audio = vlc_preview = None
    vlc_aspect = None
    toolbar = Toolbar()
    # Note this can only ever be the CAPTURE stream. The card's HDMI
    # passthrough is a hardware path from the card to a display and never
    # reaches the PC, so it cannot be drawn here - passthrough is the better
    # picture (4K60/1440p120, lag-free) but it is not available to software.
    want_preview = not (args.no_preview or args.no_capture)
    if _window is not None and want_preview:
        vids, auds = list_dshow_devices()
        vname = pick_capture(args.capture, vids)
        aname = pick_capture(args.capture_audio, auds)

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
                if aname:
                    print(f"    audio: {aname} -> default Windows output")
                vlc_preview = VlcPreview(hwnd, vname, aname, mode, hdr=args.hdr)
                toolbar.hdr = args.hdr
                if vlc_preview.error:
                    print(f"[!] VLC backend failed: {vlc_preview.error}")
                    print("    Falling back to the ffmpeg pipe (lower resolution).")
                    vlc_preview = None
                else:
                    # Keep the window at the source's aspect so the capture
                    # fills it exactly - no letterbox bars, no stretching.
                    toolbar.set_sources(vids, list_dshow_modes(vname), vname, mode)
                    if mode and mode[0] and mode[1]:
                        vlc_aspect = mode[0] / mode[1]
                        if args.fullscreen:
                            toggle_borderless_fullscreen(True)
                        # Size the window so the video AREA is the source
                        # aspect, with the toolbar strip added on top of that.
                        snap_window_to_aspect(vlc_aspect,
                                              0 if is_fullscreen() else TOOLBAR_H)
                        layout_video_child(show_toolbar=not is_fullscreen())
                        vlc_preview.fit(video_child_size(not is_fullscreen()))
                        print("    F11 = borderless fullscreen, Esc = leave it")
                        print("    toolbar: pick input device and capture mode")
                    vname = None      # handled; skip the ffmpeg path below
                    aname = None

        if vname:
            try:
                pw, ph = (int(x) for x in args.capture_size.lower().split("x"))
            except Exception:
                print(f"[-] Bad --capture-size '{args.capture_size}', using default")
                pw, ph = CAPTURE_W, CAPTURE_H
            print(f"[+] Capture video: {vname} -> {pw}x{ph} @{args.capture_fps}fps")
            capture = CapturePreview(vname, pw, ph, args.capture_fps,
                                     args.capture_input_size, args.capture_input_fps)
            if capture.error:
                print(f"[!] Capture video failed: {capture.error}")
                capture = None
        elif args.capture:
            print(f"[-] No capture device matching '{args.capture}' "
                  f"(try --list-capture)")

        # Only needed on the ffmpeg path; VLC plays its own audio.
        if aname:
            print(f"[+] Capture audio: {aname} -> default Windows output")
            capture_audio = CaptureAudio(aname)
            if capture_audio.error:
                print(f"[!] Capture audio failed: {capture_audio.error}")
                capture_audio = None

    def _slot_state():
        """Current slot assignment in the shape the toolbar wants."""
        out = {}
        for s in range(NUM_SLOTS):
            entries = slot_sources.get(s)
            if not entries:
                out[s] = None
            elif entries[0][0] == "keyboard":
                out[s] = "keyboard"
            else:
                out[s] = next((i for i, (c, _n) in opened_pads.items()
                               if c is entries[0][1][0]), None)
        return out

    # Seed the toolbar with the real pad list and current assignment, so P1-P4
    # show the truth from the first frame rather than after the first click.
    if _window is not None:
        toolbar.set_inputs(list_real_pads(), _slot_state())
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
        snap_window_to_aspect(vlc_aspect,
                              0 if is_fullscreen() else TOOLBAR_H)
        layout_video_child(show_toolbar=not is_fullscreen())
        if vlc_preview:
            vlc_preview.fit(video_child_size(not is_fullscreen()))
        refit_until = time.monotonic() + 0.6

    def _swap_vlc(make_player):
        """Replace the VLC player, pumping messages during teardown.

        The pump is load-bearing: stop() cannot finish unless the
        window's message loop keeps running (see stop_async)."""
        nonlocal vlc_preview
        if vlc_preview is not None:
            done = vlc_preview.stop_async()
            deadline = time.monotonic() + 4.0
            while not done.is_set() and time.monotonic() < deadline:
                pump_window(vlc_active=True)
                time.sleep(0.01)
        vlc_preview = make_player()
        if vlc_preview.error:
            print(f"[!] {vlc_preview.error}")
        _refit()

    def _on_click(pos):
        """Toolbar click: switch input device or capture mode.

        Both require tearing down and restarting VLC - DirectShow
        negotiates the device and mode when the stream opens, so
        they cannot be changed on a live one."""
        nonlocal vlc_preview, vlc_aspect, refit_until
        if is_fullscreen() or vlc_preview is None:
            return
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
            nonlocal recorder
            aud = pick_capture(args.capture_audio, list_dshow_devices()[1])
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
                _swap_vlc(lambda: VlcPreview(
                    _video_child, toolbar.device, aud, toolbar.mode,
                    hdr=toolbar.hdr,
                    record_path=os.path.join(d, "capture.avi")))
                if vlc_preview.error:
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
                _swap_vlc(lambda: VlcPreview(_video_child, toolbar.device, aud,
                                             toolbar.mode, hdr=toolbar.hdr))
                if recorder is not None:
                    rows, length = recorder.rows, recorder.elapsed()
                    recorder.close()
                    where = finish_recording_dir(recorder.dir, length)
                    recorder = None
                    print(f"[+] Recording saved: {where}  "
                          f"({rows} input rows)")
            return

        if toolbar.recording and kind in ("device", "mode", "hdr"):
            # Restarting the stream is how the device, mode and HDR settings
            # are applied, and that same restart truncates the file being
            # written. Refused rather than silently losing the take.
            print("[-] Stop recording before changing the capture settings.")
            return

        if kind == "slot":
            # Reassign a player slot live. Rebuilding slot_sources
            # also changes enabled_mask, which is what tells the
            # master which targets to poll at all.
            nonlocal enabled_mask, active_slots
            slot, src = value
            if src is None:
                slot_sources.pop(slot, None)
            elif src == "keyboard":
                slot_sources[slot] = [("keyboard", live_keymap,
                                       "keyboard")]
            else:
                c, name = open_pad_once(src)
                slot_sources[slot] = [("pad", (c, dict(DEFAULT_PAD_BUTTONS)),
                                       f"[{src}] {name}")]
            active_slots = sorted(slot_sources)
            enabled_mask = 0
            for s in active_slots:
                enabled_mask |= (1 << s)
            toolbar.set_inputs(toolbar.pads, _slot_state())
            print(f"[+] Controller {slot + 1} -> "
                  f"{'disabled' if src is None else src}")
            return

        if kind == "hdr":
            toolbar.hdr = value
            print(f"[+] HDR {'on (passthrough)' if value else 'off (tone mapped)'}")
            aud = pick_capture(args.capture_audio, list_dshow_devices()[1])
            _swap_vlc(lambda: VlcPreview(_video_child, toolbar.device, aud,
                                         toolbar.mode, hdr=value))
            return

        new_dev = value if kind == "device" else toolbar.device
        new_modes = list_dshow_modes(new_dev) if kind == "device" else None
        new_mode = (pick_best_mode(new_modes) if kind == "device" else value)

        print(f"[+] Switching {kind} -> "
              f"{value if kind == 'device' else mode_label(value)}")
        aud = pick_capture(args.capture_audio, list_dshow_devices()[1])
        _swap_vlc(lambda: VlcPreview(_video_child, new_dev, aud, new_mode,
                                     hdr=toolbar.hdr))
        toolbar.set_sources(toolbar.devices,
                            new_modes if new_modes is not None
                            else (toolbar.modes_comp + toolbar.modes_raw),
                            new_dev, new_mode)
        if new_mode:
            vlc_aspect = new_mode[0] / new_mode[1]
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



    try:
        while True:
            if is_key_down(VK_ESC):
                print("\n[+] Exiting test bridge.")
                break

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
                    if not pump_window(vlc_active=(vlc_preview is not None),
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
                if not pump_window(vlc_active=(vlc_preview is not None),
                                   on_resize=(_refit if vlc_preview else None),
                                   on_click=_on_click, on_key=_on_key):
                    print("\n[+] Window closed - exiting.")
                    break
                if vlc_preview is not None:
                    # VLC owns the child window's pixels, but the toolbar strip
                    # above it is still ours. Hidden in fullscreen, where the
                    # video child covers the whole window.
                    if time.monotonic() < refit_until:
                        vlc_preview.fit(video_child_size(not is_fullscreen()))
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

            # 500 Hz loop. This was 20ms (50 Hz), which by itself put up to
            # 20ms of latency on every input - far more than the whole SPI
            # path, which delivers a packet every 1ms. The master polls at
            # 1kHz, so feeding it faster than 50 Hz is what actually makes
            # the controller feel responsive.
            time.sleep(0.002)

    except KeyboardInterrupt:
        print("\n[+] Interrupted.")
    except Exception as e:
        print(f"\n[+] Unexpected error caught: {e}")
    finally:
        # Stop the players before finalising, so VLC has closed the video file
        # by the time the folder is renamed. Quitting mid-recording still
        # leaves a complete, correctly named take rather than a stray folder.
        for obj in (capture, capture_audio, vlc_preview):
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
