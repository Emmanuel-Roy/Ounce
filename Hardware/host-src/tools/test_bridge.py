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

def is_key_down(vk):
    return (ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000) != 0


def _merge_axis(a, b):
    """Combine two readings of the same axis: whichever is pushed further from
    centre wins. A resting stick (128) therefore never cancels a key press,
    and a moved stick overrides an untouched keyboard."""
    return a if abs(a - 128) >= abs(b - 128) else b


def merge_inputs(kb, pad):
    """Merge keyboard and controller state. Buttons are OR'd so either source
    can press anything; axes take whichever is displaced further."""
    if pad is None:
        return kb
    kb_b, kb_lx, kb_ly, kb_rx, kb_ry, kb_aux = kb
    pd_b, pd_lx, pd_ly, pd_rx, pd_ry, pd_aux = pad
    return (
        kb_b | pd_b,
        _merge_axis(kb_lx, pd_lx),
        _merge_axis(kb_ly, pd_ly),
        _merge_axis(kb_rx, pd_rx),
        _merge_axis(kb_ry, pd_ry),
        kb_aux | pd_aux,
    )


def read_keyboard():
    """Read the keyboard map into (buttons, lx, ly, rx, ry, aux)."""
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


def read_controller(c, deadzone, trigger_threshold):
    """Read a controller into (buttons, lx, ly, rx, ry, aux)."""
    P = _pg
    _pg.event.pump()
    b = 0

    # D-pad
    if c.get_button(P.CONTROLLER_BUTTON_DPAD_UP):    b |= SPI_MASK_UP
    if c.get_button(P.CONTROLLER_BUTTON_DPAD_DOWN):  b |= SPI_MASK_DOWN
    if c.get_button(P.CONTROLLER_BUTTON_DPAD_LEFT):  b |= SPI_MASK_LEFT
    if c.get_button(P.CONTROLLER_BUTTON_DPAD_RIGHT): b |= SPI_MASK_RIGHT

    # Face buttons, mapped by physical position rather than by letter, so a
    # DualSense cross (bottom) lands on the Switch's B (bottom) and so on.
    if c.get_button(P.CONTROLLER_BUTTON_A): b |= SPI_MASK_B1   # bottom
    if c.get_button(P.CONTROLLER_BUTTON_B): b |= SPI_MASK_B2   # right
    if c.get_button(P.CONTROLLER_BUTTON_X): b |= SPI_MASK_B3   # left
    if c.get_button(P.CONTROLLER_BUTTON_Y): b |= SPI_MASK_B4   # top

    if c.get_button(P.CONTROLLER_BUTTON_LEFTSHOULDER):  b |= SPI_MASK_L1
    if c.get_button(P.CONTROLLER_BUTTON_RIGHTSHOULDER): b |= SPI_MASK_R1

    # The Switch Pro's ZL/ZR are digital, so analog triggers get thresholded.
    if c.get_axis(P.CONTROLLER_AXIS_TRIGGERLEFT)  > trigger_threshold: b |= SPI_MASK_L2
    if c.get_axis(P.CONTROLLER_AXIS_TRIGGERRIGHT) > trigger_threshold: b |= SPI_MASK_R2

    if c.get_button(P.CONTROLLER_BUTTON_BACK):       b |= SPI_MASK_S1   # minus
    if c.get_button(P.CONTROLLER_BUTTON_START):      b |= SPI_MASK_S2   # plus
    if c.get_button(P.CONTROLLER_BUTTON_LEFTSTICK):  b |= SPI_MASK_L3
    if c.get_button(P.CONTROLLER_BUTTON_RIGHTSTICK): b |= SPI_MASK_R3

    aux = 0
    if c.get_button(P.CONTROLLER_BUTTON_GUIDE):
        aux |= SPI_AUX_MASK_HOME
    # Share/Capture is MISC1 in SDL and is absent on some pads.
    try:
        if c.get_button(P.CONTROLLER_BUTTON_MISC1):
            aux |= SPI_AUX_MASK_CAPTURE
    except Exception:
        pass

    lx = _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_LEFTX), deadzone)
    ly = _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_LEFTY), deadzone)
    rx = _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_RIGHTX), deadzone)
    ry = _axis_to_byte(c.get_axis(P.CONTROLLER_AXIS_RIGHTY), deadzone)
    return b, lx, ly, rx, ry, aux

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

def make_serial_packet(target_id, buttons, lx, ly, rx, ry, aux=0):
    flags = (target_id & SPI_TARGET_ID_MASK) | aux
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
    parser.add_argument("--input", choices=["auto", "keyboard", "gamepad"], default="auto",
                        help="Input source. 'auto' uses a physical controller if one is connected, else the keyboard.")
    parser.add_argument("--controller", type=int, default=None,
                        help="Controller index to use (see --list-controllers). Default: auto-pick.")
    parser.add_argument("--list-controllers", action="store_true",
                        help="List detected controllers and exit.")
    parser.add_argument("--deadzone", type=int, default=3000,
                        help="Stick deadzone in SDL axis units (0..32767). Default 3000.")
    parser.add_argument("--trigger-threshold", type=float, default=0.5,
                        help="Analog trigger level counted as a ZL/ZR press (0..1). Default 0.5.")
    args = parser.parse_args()

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

    controller = None
    if args.input in ("auto", "gamepad"):
        controller = open_controller(args.controller)
        if controller is None:
            if args.input == "gamepad":
                print("[-] --input gamepad requested but no usable controller. Exiting.")
                sys.exit(1)
            print("[+] Falling back to keyboard input.")

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

    print("[+] Connected! Transmitting real-time momentary controls to Target %d..." % args.target)
    print("\n--- Switch Pro Control Mapping ---")
    print("  WASD           : Left Analog Stick (Up, Down, Left, Right)")
    print("  7 8 9 0        : Right Analog Stick (Left, Up, Right, Down)")
    print("  Arrow Keys     : D-Pad (Up, Down, Left, Right)")
    print("  I              : X (Top Face Button)")
    print("  J              : Y (Left Face Button)")
    print("  K              : B (Bottom Face Button)")
    print("  L              : A (Right Face Button)")
    print("  Y              : ZL Trigger (L2)")
    print("  U              : L Bumper (L1)")
    print("  O              : R Bumper (R1)")
    print("  P              : ZR Trigger (R2)")
    print("  N / M          : Start (+) / Select (-)")
    print("  Z / X          : L3 / R3 Stick Clicks")
    print("  H / C          : Home / Capture")
    print("  ESC            : Exit Bridge")
    print("------------------------------------------")
    if controller is not None:
        print("[+] KEYBOARD + CONTROLLER both live - either can drive any input.")
        print("    On the pad: Guide = Home, Share/Capture = Capture.")
    else:
        print("[+] SILENT GAMEPLAY MODE ACTIVE.")
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

            # 1. Read every available source and merge them, so the keyboard
            # and a physical controller both stay live at the same time.
            pad_state = None
            if controller is not None:
                try:
                    pad_state = read_controller(
                        controller, args.deadzone, args.trigger_threshold)
                except Exception as e:
                    print(f"[-] Controller read failed ({e}); ignoring controller.")
            buttons, lx, ly, rx, ry, aux = merge_inputs(read_keyboard(), pad_state)

            # 3. Transmit packet over USB serial with write timeout protection
            try:
                packet = make_serial_packet(args.target, buttons, lx, ly, rx, ry, aux)
                ser.write(packet)
                # No flush() here. pyserial's Windows flush() is an unbounded
                # busy-wait on the OS output queue (no timeout, write_timeout
                # does not apply), which adds latency and jitter to every poll
                # and can block outright if the device stalls.
                total_sent += 1
            except Exception:
                time.sleep(0.01)

            # 4. Drain CDC telemetry and show it
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
