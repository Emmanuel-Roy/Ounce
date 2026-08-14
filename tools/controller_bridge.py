#!/usr/bin/env python3
"""
Ounce Universal Controller & Keyboard Bridge (controller_bridge.py)
Extends test_bridge.py to support Native XInput, Steam Input, DirectInput,
and Keyboard inputs simultaneously, transmitting real-time 50Hz momentary
controller packets to the Ounce Primary RP2350 Master for slave dispatch.
"""

import sys
import time
import struct
import argparse
import ctypes
from ctypes import wintypes
import serial
import serial.tools.list_ports

# Try importing pygame for SDL2 / Steam Input / DirectInput gamepads
try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

# ==============================================================================
# OUNCE PROTOCOL CONSTANTS & MASKS (Matching GP2040-CE Logical Mapping)
# ==============================================================================
SPI_MASK_UP    = (1 << 0)
SPI_MASK_DOWN  = (1 << 1)
SPI_MASK_LEFT  = (1 << 2)
SPI_MASK_RIGHT = (1 << 3)

SPI_MASK_B1 = (1 << 4)  # B (Bottom Face Button)
SPI_MASK_B2 = (1 << 5)  # A (Right Face Button)
SPI_MASK_B3 = (1 << 6)  # Y (Left Face Button)
SPI_MASK_B4 = (1 << 7)  # X (Top Face Button)

SPI_MASK_L1 = (1 << 8)  # L (Left Bumper)
SPI_MASK_R1 = (1 << 9)  # R (Right Bumper)
SPI_MASK_L2 = (1 << 10) # ZL (Left Trigger)
SPI_MASK_R2 = (1 << 11) # ZR (Right Trigger)

SPI_MASK_S1 = (1 << 12) # Select (-)
SPI_MASK_S2 = (1 << 13) # Start (+)

SPI_MASK_L3 = (1 << 14) # L3 (Left Stick Click)
SPI_MASK_R3 = (1 << 15) # R3 (Right Stick Click)

# Windows Virtual Key Codes for Instant Momentary Keyboard Controls
VK_W = 0x57; VK_S = 0x53; VK_A = 0x41; VK_D = 0x44
VK_UP = 0x26; VK_DOWN = 0x28; VK_LEFT = 0x25; VK_RIGHT = 0x27
VK_I = 0x49; VK_J = 0x4A; VK_K = 0x4B; VK_L = 0x4C
VK_Y = 0x59; VK_U = 0x55; VK_O = 0x4F; VK_P = 0x50
VK_N = 0x4E; VK_M = 0x4D; VK_Z = 0x5A; VK_X = 0x58
VK_ESC = 0x1B

def is_key_down(vk):
    return (ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000) != 0

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

def make_serial_packet(target_id, buttons, lx, ly, rx):
    payload = struct.pack('<BBHBBB', 0x5A, target_id, buttons, lx, ly, rx)
    crc = calculate_crc8(payload)
    return payload + bytes([crc])

# ==============================================================================
# FAST PORT DETECTION (Bypasses slow Windows Bluetooth enumeration)
# ==============================================================================
def find_primary_port():
    if sys.platform == 'win32':
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DEVICEMAP\SERIALCOMM')
            num_values = winreg.QueryInfoKey(key)[1]
            usb_ports = []
            other_ports = []
            for i in range(num_values):
                dev_name, port_name = winreg.EnumValue(key, i)[0:2]
                dev_lower = dev_name.lower()
                if "bth" in dev_lower or "bluetooth" in dev_lower:
                    continue
                if "usbser" in dev_lower:
                    usb_ports.append(port_name)
                elif "serial" not in dev_lower:
                    other_ports.append(port_name)
            winreg.CloseKey(key)
            if usb_ports:
                if 'COM5' in usb_ports:
                    return 'COM5'
                return usb_ports[0]
            if other_ports:
                return other_ports[0]
        except Exception:
            pass

    try:
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
    except Exception:
        pass
    return 'COM5'

def open_serial_connection(port_override=None):
    port_name = port_override or find_primary_port()
    if not port_name:
        return None, None
    formatted_port = f"\\\\.\\{port_name.upper()}" if port_name.upper().startswith("COM") and not port_name.startswith("\\\\.\\") else port_name
    try:
        ser = serial.Serial(formatted_port, 115200, timeout=0.05, write_timeout=0.5)
        ser.dtr = True
        ser.rts = True
        return ser, port_name
    except Exception:
        return None, port_name

# ==============================================================================
# NATIVE XINPUT CONTROLLER DRIVER (Direct CTypes)
# ==============================================================================
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ('wButtons', wintypes.WORD),
        ('bLeftTrigger', wintypes.BYTE),
        ('bRightTrigger', wintypes.BYTE),
        ('sThumbLX', wintypes.SHORT),
        ('sThumbLY', wintypes.SHORT),
        ('sThumbRX', wintypes.SHORT),
        ('sThumbRY', wintypes.SHORT),
    ]

class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ('dwPacketNumber', wintypes.DWORD),
        ('Gamepad', XINPUT_GAMEPAD),
    ]

class XInputReader:
    def __init__(self):
        self.dll = None
        self.connected_index = None
        for dll_name in ['xinput1_4.dll', 'xinput1_3.dll', 'xinput9_1_0.dll']:
            try:
                self.dll = ctypes.windll.LoadLibrary(dll_name)
                break
            except Exception:
                continue

    def poll(self, deadzone=0.15):
        if not self.dll:
            return None

        # Check connected index or find first connected controller
        indices = [self.connected_index] if self.connected_index is not None else range(4)
        for idx in indices:
            if idx is None:
                continue
            state = XINPUT_STATE()
            res = self.dll.XInputGetState(idx, ctypes.byref(state))
            if res == 0:
                self.connected_index = idx
                gp = state.Gamepad
                buttons = 0

                # D-Pad
                if gp.wButtons & 0x0001: buttons |= SPI_MASK_UP
                if gp.wButtons & 0x0002: buttons |= SPI_MASK_DOWN
                if gp.wButtons & 0x0004: buttons |= SPI_MASK_LEFT
                if gp.wButtons & 0x0008: buttons |= SPI_MASK_RIGHT

                # Start & Back/Select
                if gp.wButtons & 0x0010: buttons |= SPI_MASK_S2 # Start (+)
                if gp.wButtons & 0x0020: buttons |= SPI_MASK_S1 # Back/Select (-)

                # Stick Clicks
                if gp.wButtons & 0x0040: buttons |= SPI_MASK_L3 # L3
                if gp.wButtons & 0x0080: buttons |= SPI_MASK_R3 # R3

                # Bumpers
                if gp.wButtons & 0x0100: buttons |= SPI_MASK_L1 # L
                if gp.wButtons & 0x0200: buttons |= SPI_MASK_R1 # R

                # Face Buttons: Xbox A(bottom)=B, B(right)=A, X(left)=Y, Y(top)=X
                if gp.wButtons & 0x1000: buttons |= SPI_MASK_B1 # A -> B1 (Bottom)
                if gp.wButtons & 0x2000: buttons |= SPI_MASK_B2 # B -> B2 (Right)
                if gp.wButtons & 0x4000: buttons |= SPI_MASK_B3 # X -> B3 (Left)
                if gp.wButtons & 0x8000: buttons |= SPI_MASK_B4 # Y -> B4 (Top)

                # Analog Triggers (Threshold = 30 / 255)
                if gp.bLeftTrigger > 30:  buttons |= SPI_MASK_L2
                if gp.bRightTrigger > 30: buttons |= SPI_MASK_R2

                # Analog Sticks (-32768..32767 -> 0..255)
                def scale_stick(val, invert_y=False):
                    norm = val / 32768.0
                    if abs(norm) < deadzone:
                        return 128
                    if invert_y:
                        norm = -norm
                    scaled = int(128 + norm * 127)
                    return max(0, min(255, scaled))

                lx = scale_stick(gp.sThumbLX, invert_y=False)
                ly = scale_stick(gp.sThumbLY, invert_y=True) # XInput UP is positive, protocol 0 is UP
                rx = scale_stick(gp.sThumbRX, invert_y=False)

                return buttons, lx, ly, rx, f"XInput (Port {idx})"

        self.connected_index = None
        return None

# ==============================================================================
# PYGAME / STEAM INPUT / DIRECTINPUT CONTROLLER DRIVER
# ==============================================================================
class PygameReader:
    def __init__(self):
        self.initialized = False
        self.joysticks = []
        if PYGAME_AVAILABLE:
            try:
                pygame.init()
                pygame.joystick.init()
                self.initialized = True
                self._refresh_joysticks()
            except Exception:
                self.initialized = False

    def _refresh_joysticks(self):
        if not self.initialized:
            return
        self.joysticks = []
        for i in range(pygame.joystick.get_count()):
            try:
                js = pygame.joystick.Joystick(i)
                js.init()
                self.joysticks.append(js)
            except Exception:
                pass

    def poll(self, deadzone=0.15):
        if not self.initialized:
            return None

        try:
            pygame.event.pump()
        except Exception:
            return None

        if pygame.joystick.get_count() != len(self.joysticks):
            self._refresh_joysticks()

        if not self.joysticks:
            return None

        # Poll primary joystick
        js = self.joysticks[0]
        buttons = 0
        name = js.get_name()

        num_btns = js.get_numbuttons()
        num_axes = js.get_numaxes()
        num_hats = js.get_numhats()

        # Standard SDL2 / GameController Mapping:
        # 0: A (Bottom) -> B1
        # 1: B (Right)  -> B2
        # 2: X (Left)   -> B3
        # 3: Y (Top)    -> B4
        if num_btns > 0 and js.get_button(0): buttons |= SPI_MASK_B1
        if num_btns > 1 and js.get_button(1): buttons |= SPI_MASK_B2
        if num_btns > 2 and js.get_button(2): buttons |= SPI_MASK_B3
        if num_btns > 3 and js.get_button(3): buttons |= SPI_MASK_B4

        # Back (4) / Start (6)
        if num_btns > 4 and js.get_button(4): buttons |= SPI_MASK_S1
        if num_btns > 6 and js.get_button(6): buttons |= SPI_MASK_S2

        # Bumpers (9=L1, 10=R1) or alternate layout (4=L1, 5=R1)
        if num_btns > 9 and js.get_button(9): buttons |= SPI_MASK_L1
        elif num_btns > 4 and num_btns <= 10 and js.get_button(4): buttons |= SPI_MASK_L1

        if num_btns > 10 and js.get_button(10): buttons |= SPI_MASK_R1
        elif num_btns > 5 and num_btns <= 10 and js.get_button(5): buttons |= SPI_MASK_R1

        # Stick Clicks (7=L3, 8=R3)
        if num_btns > 7 and js.get_button(7): buttons |= SPI_MASK_L3
        if num_btns > 8 and js.get_button(8): buttons |= SPI_MASK_R3

        # D-Pad from Hat switch (if present)
        if num_hats > 0:
            hat = js.get_hat(0)
            if hat[1] == 1:  buttons |= SPI_MASK_UP
            if hat[1] == -1: buttons |= SPI_MASK_DOWN
            if hat[0] == -1: buttons |= SPI_MASK_LEFT
            if hat[0] == 1:  buttons |= SPI_MASK_RIGHT
        elif num_btns >= 15:
            # D-Pad on extended buttons (11..14)
            if js.get_button(11): buttons |= SPI_MASK_UP
            if js.get_button(12): buttons |= SPI_MASK_DOWN
            if js.get_button(13): buttons |= SPI_MASK_LEFT
            if js.get_button(14): buttons |= SPI_MASK_RIGHT

        # Analog Triggers (Axes 4 and 5, or 2 and 5)
        if num_axes >= 6:
            # Trigger axes commonly rest at -1.0 and press to +1.0
            if js.get_axis(4) > 0.0: buttons |= SPI_MASK_L2
            if js.get_axis(5) > 0.0: buttons |= SPI_MASK_R2

        # Analog Sticks (Axis 0=LX, Axis 1=LY, Axis 2 or 3=RX)
        def scale_axis(val):
            if abs(val) < deadzone:
                return 128
            scaled = int(128 + val * 127)
            return max(0, min(255, scaled))

        lx = 128; ly = 128; rx = 128
        if num_axes >= 2:
            lx = scale_axis(js.get_axis(0))
            ly = scale_axis(js.get_axis(1)) # In Pygame, +1 is Down, -1 is Up
        if num_axes >= 3:
            rx_axis_idx = 2 if num_axes == 4 else (3 if num_axes >= 4 else 2)
            rx = scale_axis(js.get_axis(rx_axis_idx))

        return buttons, lx, ly, rx, f"Steam/SDL2 ({name})"

# ==============================================================================
# KEYBOARD INPUT DRIVER
# ==============================================================================
def poll_keyboard():
    buttons = 0
    lx, ly = 128, 128

    if is_key_down(VK_A): lx = 0
    elif is_key_down(VK_D): lx = 255

    if is_key_down(VK_W): ly = 0
    elif is_key_down(VK_S): ly = 255

    rx = 128

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
    if is_key_down(VK_Y): buttons |= SPI_MASK_L2  # Y = ZL Trigger (L2)
    if is_key_down(VK_U): buttons |= SPI_MASK_L1  # U = L Bumper (L1)
    if is_key_down(VK_O): buttons |= SPI_MASK_R1  # O = R Bumper (R1)
    if is_key_down(VK_P): buttons |= SPI_MASK_R2  # P = ZR Trigger (R2)

    # Start & Select (N / M)
    if is_key_down(VK_N): buttons |= SPI_MASK_S2  # Start (+)
    if is_key_down(VK_M): buttons |= SPI_MASK_S1  # Select (-)

    # Stick Clicks (Z / X)
    if is_key_down(VK_Z): buttons |= SPI_MASK_L3  # L3 Click
    if is_key_down(VK_X): buttons |= SPI_MASK_R3  # R3 Click

    return buttons, lx, ly, rx

# ==============================================================================
# MAIN BRIDGE EXECUTION
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Ounce Universal Controller & Keyboard Bridge")
    parser.add_argument("--port", type=str, default=None, help="Serial port of Primary RP2350 Master (e.g. COM5)")
    parser.add_argument("--target", type=int, default=0, help="Target Slave ID (0..3)")
    parser.add_argument("--max-packets", type=int, default=0, help="Exit automatically after transmitting N packets (0 = run continuously)")
    parser.add_argument("--deadzone", type=float, default=0.15, help="Analog stick deadzone (default: 0.15)")
    args = parser.parse_args()

    print("=================================================================")
    print("      OUNCE UNIVERSAL CONTROLLER & KEYBOARD BRIDGE               ")
    print("=================================================================")
    print("  Input Drivers Active:")
    print("    * Native XInput (Xbox Series/One/360 / Virtual XInput)")
    print("    * Steam Input & SDL2 Gamepad (Switch Pro / DualSense / DirectInput)")
    print("    * Real-Time Keyboard (WASD / Arrow Keys / IJKL Face Buttons)")
    print("-----------------------------------------------------------------")
    print("  Keyboard Hotkeys:")
    print("    WASD / Arrows  : Left Stick / D-Pad")
    print("    I / J / K / L  : X / Y / B / A")
    print("    U / O          : L1 / R1 (Bumpers)")
    print("    Y / P          : L2 / R2 (Triggers)")
    print("    N / M          : Start (+) / Select (-)")
    print("    Z / X          : L3 / R3")
    print("    ESC            : Exit Bridge")
    print("=================================================================\n")

    xinput_driver = XInputReader()
    pygame_driver = PygameReader()

    total_sent = 0
    text_buf = ""
    ser = None
    last_device_name = ""

    try:
        while True:
            # 1. Maintain active serial connection
            if ser is None:
                print(f"[+] Searching for Primary RP2350 Master board...")
                while ser is None:
                    if is_key_down(VK_ESC):
                        print("\n[+] Exiting controller bridge.")
                        return
                    ser, actual_port = open_serial_connection(args.port)
                    if ser is None:
                        time.sleep(0.5)
                print(f"[+] Connected to Primary RP2350 on {actual_port} @ 115200 baud!")
                print(f"[+] Streaming controller inputs to Target {args.target}...\n")
                text_buf = ""

            if is_key_down(VK_ESC):
                print("\n[+] Exiting controller bridge.")
                break

            if args.max_packets > 0 and total_sent >= args.max_packets:
                print(f"\n[+] Reached maximum packet limit of {args.max_packets}. Exiting.")
                break

            # 2. Gather inputs from all drivers
            final_buttons = 0
            final_lx, final_ly, final_rx = 128, 128, 128
            active_device = "Keyboard"

            # (a) Keyboard inputs
            kb_btns, kb_lx, kb_ly, kb_rx = poll_keyboard()
            final_buttons |= kb_btns
            if kb_lx != 128 or kb_ly != 128:
                final_lx, final_ly = kb_lx, kb_ly
            if kb_rx != 128:
                final_rx = kb_rx

            # (b) XInput Native Controller
            xinput_data = xinput_driver.poll(deadzone=args.deadzone)
            if xinput_data:
                xi_btns, xi_lx, xi_ly, xi_rx, xi_name = xinput_data
                final_buttons |= xi_btns
                if xi_lx != 128 or xi_ly != 128:
                    final_lx, final_ly = xi_lx, xi_ly
                if xi_rx != 128:
                    final_rx = xi_rx
                active_device = xi_name

            # (c) Pygame / Steam Input / DirectInput Controller
            pg_data = pygame_driver.poll(deadzone=args.deadzone)
            if pg_data:
                pg_btns, pg_lx, pg_ly, pg_rx, pg_name = pg_data
                final_buttons |= pg_btns
                if pg_lx != 128 or pg_ly != 128:
                    final_lx, final_ly = pg_lx, pg_ly
                if pg_rx != 128:
                    final_rx = pg_rx
                active_device = pg_name

            if active_device != last_device_name and active_device != "Keyboard":
                print(f"[+] Active Gamepad: {active_device}")
                last_device_name = active_device

            # 3. Transmit packet over USB serial
            try:
                packet = make_serial_packet(args.target, final_buttons, final_lx, final_ly, final_rx)
                ser.write(packet)
                total_sent += 1
            except serial.SerialTimeoutException:
                time.sleep(0.005)
            except (serial.SerialException, OSError, PermissionError) as e:
                print(f"\n[!] Link disconnected ({e}). Reconnecting...")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                time.sleep(0.5)
                continue

            # 4. Drain CDC telemetry non-blockingly
            try:
                n_avail = ser.in_waiting
                if n_avail > 0:
                    chunk = ser.read(n_avail).decode('utf-8', errors='ignore')
                    text_buf += chunk
                    while '\n' in text_buf:
                        line, text_buf = text_buf.split('\n', 1)
                        line = line.strip()
                        if line:
                            print(line)
            except (serial.SerialException, OSError, PermissionError) as e:
                print(f"\n[!] Read failed ({e}). Reconnecting...")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                time.sleep(0.5)
                continue

            time.sleep(0.02) # 50 Hz loop (20 ms interval)

    except KeyboardInterrupt:
        print("\n[+] Interrupted by user.")
    except Exception as e:
        print(f"\n[+] Unexpected error caught: {e}")
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

if __name__ == '__main__':
    main()
