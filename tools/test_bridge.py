import sys
import time
import struct
import argparse
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

    def __init__(self, hwnd, video_dev, audio_dev=None, mode=None):
        self.error = None
        self._player = None
        self._inst = None
        try:
            import vlc
        except Exception:
            self.error = "python-vlc not installed (pip install python-vlc)"
            return
        try:
            # --no-xlib is harmless on Windows; the rest keeps latency down.
            self._inst = vlc.Instance([
                "--no-video-title-show",
                "--quiet",
                "--network-caching=0",
                "--live-caching=0",
                "--file-caching=0",
            ])
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
                # VLC wants a FourCC here. MJPEG is the important one: it is
                # the only format offering 4K60 / 1440p144 / 1080p240, because
                # uncompressed at those rates will not fit over USB.
                chroma = {"mjpeg": "MJPG", "yuyv422": "YUY2",
                          "yuv420p": "I420", "nv12": "NV12"}.get(pixfmt, pixfmt)
                opts += [f":dshow-size={w}x{h}",
                         f":dshow-fps={fps}",
                         f":dshow-chroma={chroma}"]
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

    def source_size(self):
        try:
            return self._player.video_get_size(0)
        except Exception:
            return (0, 0)

    def is_playing(self):
        try:
            return bool(self._player and self._player.is_playing())
        except Exception:
            return False

    def stop(self):
        for obj, meth in ((self._player, "stop"), (self._inst, "release")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass


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


def snap_window_to_aspect(aspect):
    """Force the window's client area to `aspect`, keeping its width.

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
        want_client_h = int(round(client_w / aspect))
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


def pump_window(vlc_active=False, on_resize=None):
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

        # Steam switches a controller out of its Desktop (keyboard/mouse)
        # configuration when it detects the *game* - which it does by hooking a
        # real window. A windowless process never triggers that switch, leaving
        # the pad stuck in mouse mode no matter what layout is configured. So
        # under Steam we create an actual window; standalone we stay headless.
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
                        help="Force the preview window even when not launched by Steam.")
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

    if args.window:
        import os
        os.environ["OUNCE_WINDOW"] = "1"
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

    # Capture card preview. Only meaningful when a window exists, which is the
    # Steam case - the window has to be there for Steam Input anyway, so we may
    # as well put the game on it.
    capture = capture_audio = vlc_preview = None
    vlc_aspect = None
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
                mode = pick_best_mode(list_dshow_modes(vname), args.capture_mode)
                print(f"[+] Capture (VLC, GPU): {vname}")
                if mode:
                    print(f"    mode: {mode[0]}x{mode[1]} @{mode[2]}fps {mode[3]}"
                          f"   (--list-modes for alternatives)")
                else:
                    print("    mode: card advertised none; letting DirectShow choose")
                if aname:
                    print(f"    audio: {aname} -> default Windows output")
                vlc_preview = VlcPreview(hwnd, vname, aname, mode)
                if vlc_preview.error:
                    print(f"[!] VLC backend failed: {vlc_preview.error}")
                    print("    Falling back to the ffmpeg pipe (lower resolution).")
                    vlc_preview = None
                else:
                    # Keep the window at the source's aspect so the capture
                    # fills it exactly - no letterbox bars, no stretching.
                    if mode and mode[0] and mode[1]:
                        vlc_aspect = mode[0] / mode[1]
                        snap_window_to_aspect(vlc_aspect)
                        if args.fullscreen:
                            toggle_borderless_fullscreen(True)
                        cs = window_client_size()
                        vlc_preview.fit(cs)
                        print(f"    client {cs[0]}x{cs[1]} filled edge to edge"
                              f"{' (borderless fullscreen)' if is_fullscreen() else ''}")
                        print("    F11 = borderless fullscreen, Esc = leave it")
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

    total_sent = 0
    start_time = time.monotonic()
    last_paint = 0.0
    refit_until = 0.0      # keep re-fitting VLC until this time (see _refit)

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

            # Repaint the window at ~30fps. Deliberately decoupled from the
            # input loop above, which runs at 500Hz: painting is far more
            # expensive than building a packet and must never pace it.
            if _window is not None and (time.monotonic() - last_paint) >= 0.033:
                last_paint = time.monotonic()
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
                    snap_window_to_aspect(vlc_aspect)
                    if vlc_preview:
                        vlc_preview.fit(window_client_size())
                    refit_until = time.monotonic() + 0.6

                if not pump_window(vlc_active=(vlc_preview is not None),
                                   on_resize=(_refit if vlc_preview else None)):
                    print("\n[+] Window closed - exiting.")
                    break
                if vlc_preview is not None:
                    # VLC owns the window's pixels; do not draw over it. Keep
                    # re-fitting briefly after a resize so the async geometry
                    # change never leaves visible bars mid-transition.
                    if time.monotonic() < refit_until:
                        vlc_preview.fit(window_client_size())
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
        for obj in (capture, capture_audio, vlc_preview):
            if obj is not None:
                obj.stop()
        try:
            ser.close()
        except Exception:
            pass

if __name__ == '__main__':
    main()
