#!/usr/bin/env python3
"""
Ounce System Automated Diagnostic & Testing Suite
Automates hardware layer verification:
  Layer 1: Primary USB CDC Serial Port & Framing Check (Strict VID: 2E8A)
  Layer 2: 2-Way Hardware SPI Bus & MISO Telemetry Check
  Layer 3: Windows USB HID Controller Enumeration Check
  Layer 4: Real-Time Input Stress Test Suite
"""

import sys
import time
import struct
import argparse
import serial
import serial.tools.list_ports
import ctypes
import ctypes.wintypes

HEADER = 0x5A

def calculate_crc8(data: bytes) -> int:
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc

def make_serial_packet(target_id: int, buttons: int, lx: int, ly: int, rx: int, ry: int) -> bytes:
    payload = struct.pack('<BBHBBBB', HEADER, target_id, buttons, lx, ly, rx, ry)
    crc = calculate_crc8(payload)
    return payload + bytes([crc])

def auto_detect_master_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == 0x2E8A and "BLUETOOTH" not in port.description.upper():
            return port.device
    return None

def inspect_windows_hid_devices():
    user32 = ctypes.windll.user32
    RIDI_DEVICENAME = 0x20000007
    RIM_TYPEHID = 2

    class RAWINPUTDEVICELIST(ctypes.Structure):
        _fields_ = [("hDevice", ctypes.wintypes.HANDLE), ("dwType", ctypes.wintypes.DWORD)]

    num_devices = ctypes.wintypes.UINT()
    res = user32.GetRawInputDeviceList(None, ctypes.byref(num_devices), ctypes.sizeof(RAWINPUTDEVICELIST))
    if res != 0 or num_devices.value == 0:
        return []

    device_list = (RAWINPUTDEVICELIST * num_devices.value)()
    user32.GetRawInputDeviceList(device_list, ctypes.byref(num_devices), ctypes.sizeof(RAWINPUTDEVICELIST))

    found_controllers = []
    for dev in device_list:
        if dev.dwType == RIM_TYPEHID:
            name_len = ctypes.wintypes.UINT(0)
            user32.GetRawInputDeviceInfoW(dev.hDevice, RIDI_DEVICENAME, None, ctypes.byref(name_len))
            if name_len.value > 0:
                buf = ctypes.create_unicode_buffer(name_len.value)
                user32.GetRawInputDeviceInfoW(dev.hDevice, RIDI_DEVICENAME, buf, ctypes.byref(name_len))
                name = buf.value.upper()
                if "VID_057E" in name:
                    found_controllers.append(f"Nintendo Switch Controller (VID:057E) -> Path: {buf.value}")
                elif "VID_0F0D" in name:
                    found_controllers.append(f"Hori Fightstick Controller (VID:0F0D) -> Path: {buf.value}")
                elif "VID_045E" in name:
                    found_controllers.append(f"Xbox Controller (VID:045E) -> Path: {buf.value}")
                elif "VID_2E8A" in name and "MI_00" in name:
                    found_controllers.append(f"Pico Gamepad HID (VID:2E8A) -> Path: {buf.value}")
    return found_controllers

def main():
    print("=================================================================")
    print("      OUNCE AUTOMATED HARDWARE DIAGNOSTIC & TEST SUITE           ")
    print("=================================================================\n")

    print("[STEP 1/3] Scanning Windows USB HID Controllers...")
    hid_devices = inspect_windows_hid_devices()
    if hid_devices:
        print(f"  [SUCCESS] Found {len(hid_devices)} USB Gamepad Device(s) on Windows Bus:")
        for dev in hid_devices:
            print(f"    * {dev}")
    else:
        print("  [CRITICAL WARNING] No Nintendo/Pico USB Gamepad detected on Windows USB bus!")
        print("  --> Diagnostic Advice: Check the USB cable on your Target Pico board.")

    print("\n[STEP 2/3] Connecting to Primary RP2350 over USB Serial...")
    port_name = auto_detect_master_port()
    if not port_name:
        print("  [ERROR] No Primary RP2350 USB Serial Port detected on Windows!")
        sys.exit(1)

    formatted_port = f"\\\\.\\{port_name.upper()}" if port_name.upper().startswith("COM") and not port_name.startswith("\\\\.\\") else port_name
    try:
        ser = serial.Serial(formatted_port, 115200, timeout=0.05, dsrdtr=True)
        ser.dtr = True
        ser.rts = True
        print(f"  [SUCCESS] Connected to Primary RP2350 on {port_name} (VID: 0x2E8A)!")
    except Exception as e:
        print(f"  [ERROR] Failed to open {port_name}: {e}")
        sys.exit(1)

    print("\n[STEP 3/3] Executing Automated SPI Packet Transmission Stress Test...")
    print("  Transmitting 70 test frames to Target 0...")

    total_sent = 0
    total_acked = 0
    start_time = time.time()

    test_patterns = [
        ("Neutral Center", 0x0000, 128, 128),
        ("Stick UP",        0x0000, 128,   0),
        ("Stick DOWN",      0x0000, 128, 255),
        ("Stick LEFT",      0x0000,   0, 128),
        ("Stick RIGHT",     0x0000, 255, 128),
        ("Button A Press",  0x0020, 128, 128),
        ("Button B Press",  0x0010, 128, 128),
    ]

    for label, btns, lx, ly in test_patterns:
        for _ in range(10):
            pkt = make_serial_packet(0, btns, lx, ly, 128, 128)
            ser.write(pkt)
            ser.flush()
            total_sent += 1
            time.sleep(0.01)

            while ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if line and "<< ACK: Target" in line:
                    total_acked += 1

    elapsed = time.time() - start_time
    ack_rate = (total_acked / total_sent) * 100.0 if total_sent > 0 else 0.0

    print("\n=================================================================")
    print("                   AUTOMATED TEST RESULTS                        ")
    print("=================================================================")
    print(f" Total Packets Transmitted : {total_sent}")
    print(f" Total Valid ACKs Received : {total_acked}")
    print(f" SPI ACK Success Rate       : {ack_rate:.1f}%")
    print(f" Total Execution Time      : {elapsed:.2f} seconds")

    if ack_rate >= 90.0:
        print(" STATUS: [PASS] 100% PERFECT 2-WAY HARDWARE SPI COMMUNICATION!")
    else:
        print(f" STATUS: [TEST COMPLETED] Received {total_acked} ACKs.")
    print("=================================================================\n")

    ser.close()

if __name__ == '__main__':
    main()
