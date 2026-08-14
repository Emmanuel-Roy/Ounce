import sys
import time
import struct
import argparse
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
    if not pads:
        print("[-] No non-Ounce controller detected.")
        print("    Under Steam this usually means Steam Input is not enabled for")
        print("    this shortcut, so the pad is still in Desktop (keyboard/mouse)")
        print("    mode. Properties -> Controller -> Enable Steam Input.")
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


def read_keyboard():
    """Read the default keyboard map into (buttons, lx, ly, rx, ry, aux)."""
    # Left Analog Stick (WASD)
    lx, ly = 128, 128
    if is_key_down(VK_A): lx = 0
    elif is_key_down(VK_D): lx = 255

    if is_key_down(VK_W): ly = 0
    elif is_key_down(VK_S): ly = 255

    # Right Analog Stick (7/8/9/0)
    rx, ry = 128, 128
    if is_key_down(VK_7): rx = 0
    elif is_key_down(VK_9): rx = 255

    if is_key_down(VK_8): ry = 0
    elif is_key_down(VK_0): ry = 255

    buttons = 0

    # D-Pad (Arrow Keys)
    if is_key_down(VK_UP):    buttons |= SPI_MASK_UP
    if is_key_down(VK_DOWN):  buttons |= SPI_MASK_DOWN
    if is_key_down(VK_LEFT):  buttons |= SPI_MASK_LEFT
    if is_key_down(VK_RIGHT): buttons |= SPI_MASK_RIGHT

    # Face Buttons (I=X, J=Y, K=B, L=A)
    if is_key_down(VK_I): buttons |= SPI_MASK_B4  # X (Top)
    if is_key_down(VK_J): buttons |= SPI_MASK_B3  # Y (Left)
    if is_key_down(VK_K): buttons |= SPI_MASK_B1  # B (Bottom)
    if is_key_down(VK_L): buttons |= SPI_MASK_B2  # A (Right)

    # Bumpers & Triggers (Y=L2/ZL, U=L1/L, O=R1/R, P=R2/ZR)
    if is_key_down(VK_Y): buttons |= SPI_MASK_L2
    if is_key_down(VK_U): buttons |= SPI_MASK_L1
    if is_key_down(VK_O): buttons |= SPI_MASK_R1
    if is_key_down(VK_P): buttons |= SPI_MASK_R2

    # Start & Select (N / M)
    if is_key_down(VK_N): buttons |= SPI_MASK_S2  # Start (+)
    if is_key_down(VK_M): buttons |= SPI_MASK_S1  # Select (-)

    # Stick Clicks (Z / X)
    if is_key_down(VK_Z): buttons |= SPI_MASK_L3
    if is_key_down(VK_X): buttons |= SPI_MASK_R3

    # Home / Capture (H / C) - these live in the flags byte
    aux = 0
    if is_key_down(VK_H): aux |= SPI_AUX_MASK_HOME
    if is_key_down(VK_C): aux |= SPI_AUX_MASK_CAPTURE

    return buttons, lx, ly, rx, ry, aux


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


def gamepad_available():
    global _pg, _sdl_controller
    if _pg is not None:
        return True
    try:
        import os
        # Headless: never pop up a window just to read a joystick.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_JOYSTICK_HIDAPI_PS5", "1")   # native DualSense
        os.environ.setdefault("SDL_JOYSTICK_HIDAPI_STEAM", "1") # Steam Controller
        import pygame
        from pygame._sdl2 import controller as sdlcontroller
        pygame.init()
        sdlcontroller.init()
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
    parser.add_argument("--relaunch-seconds", type=float, default=5.0, help="Fully close and re-exec the bridge (fresh serial connection) after this many seconds, as a safety net against a stuck link. 0 disables.")
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
                        ("keyboard", dict(DEFAULT_KEYBOARD_BINDINGS), "keyboard"))
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
                        ("keyboard", dict(DEFAULT_KEYBOARD_BINDINGS), "keyboard (default map)"))
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
            legacy.append(("keyboard", dict(DEFAULT_KEYBOARD_BINDINGS), "keyboard (default map)"))
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
    if args.relaunch_seconds > 0:
        print(f"[+] Auto-relaunch enabled: fresh connection every {args.relaunch_seconds:.0f}s.\n")
    else:
        print("[+] Auto-relaunch disabled.\n")

    total_sent = 0
    start_time = time.monotonic()

    try:
        while True:
            if is_key_down(VK_ESC):
                print("\n[+] Exiting test bridge.")
                break

            if args.relaunch_seconds > 0 and (time.monotonic() - start_time) >= args.relaunch_seconds:
                # Toggle DTR instead of closing/reopening the port: a full
                # close+reopen goes through Windows' USB-CDC driver teardown
                # and re-enumeration handshake, which can take 100-500ms and
                # was the source of the visible stall. DTR toggling never
                # closes the OS handle at all - it just pulses the control
                # line - so it's sub-millisecond and still gets us the
                # "cycle the connection" effect the relaunch is for.
                print("[+] Disconnecting serial...")
                try:
                    ser.dtr = False
                    time.sleep(0.005)
                    ser.dtr = True
                except Exception:
                    pass
                print("[+] Reconnected serial.")
                start_time = time.monotonic()

            if args.max_packets > 0 and total_sent >= args.max_packets:
                print("\n[+] Reached maximum packet limit of %d. Exiting." % args.max_packets)
                break

            # 1. Build and send one packet per enabled slot. Each slot merges
            # all of its own sources, so a slot can be driven by a keyboard and
            # a pad at once, and different slots by different devices.
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

            # (Packets for every enabled slot were already sent above. No
            # flush() anywhere: pyserial's Windows flush() is an unbounded
            # busy-wait on the OS output queue, with no timeout, which adds
            # latency and jitter and can block outright if the device stalls.)

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
        try:
            ser.close()
        except Exception:
            pass

if __name__ == '__main__':
    main()
