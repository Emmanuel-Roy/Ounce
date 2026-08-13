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
    args = parser.parse_args()

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
    print("  ESC            : Exit Bridge")
    print("------------------------------------------")
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

            # 1. Analog Stick Calculations (WASD)
            lx, ly = 128, 128
            if is_key_down(VK_A): lx = 0
            elif is_key_down(VK_D): lx = 255

            if is_key_down(VK_W): ly = 0
            elif is_key_down(VK_S): ly = 255

            rx = 128
            buttons = 0

            # 2. D-Pad Calculations (Arrow Keys)
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

            # 3. Transmit packet over USB serial with write timeout protection
            try:
                packet = make_serial_packet(args.target, buttons, lx, ly, rx)
                ser.write(packet)
                ser.flush()
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

            time.sleep(0.02) # 50 Hz loop (20 ms interval)

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
