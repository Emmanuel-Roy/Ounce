#!/usr/bin/env python3
"""
Ounce Automated UF2 Flasher
Monitors Windows drive letters for RP2040 / RP2350 BOOTSEL drives (RPI-RP2 or RP2350)
and automatically flashes the latest UF2 binaries upon detection.
"""

import sys
import time
import os
import shutil
import ctypes

SLAVE_UF2  = r"Z:\Code\Github\Ounce\bin\GP2040-CE_v0.7.8_Pico.uf2"
MASTER_UF2 = r"Z:\Code\Github\Ounce\bin\ounce_master.uf2"

def get_drive_labels():
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        if bitmask & 1:
            drive_path = f"{letter}:\\"
            buf = ctypes.create_unicode_buffer(261)
            res = ctypes.windll.kernel32.GetVolumeInformationW(
                drive_path, buf, ctypes.sizeof(buf), None, None, None, None, 0
            )
            if res:
                drives.append((letter, buf.value))
        bitmask >>= 1
    return drives

def main():
    print("[+] Ounce Automated Pico / RP2350 UF2 Flasher Active...")
    print("[!] Plug in or reset any board with BOOTSEL held down. Flasher will auto-detect and write firmware.\n")

    flashed = set()

    try:
        while True:
            drives = get_drive_labels()
            for letter, label in drives:
                drive_key = f"{letter}:{label}"
                if drive_key in flashed:
                    continue

                if "RPI-RP2" in label.upper() or "RP2040" in label.upper():
                    print(f"\n[+] DETECTED SLAVE BOOTSEL DRIVE on {letter}:\\ ({label})")
                    target = f"{letter}:\\GP2040-CE_v0.7.8_Pico.uf2"
                    print(f"    --> Copying {SLAVE_UF2} to {target}...")
                    shutil.copyfile(SLAVE_UF2, target)
                    print(f"  [SUCCESS] Slave RP2040 Flashed Successfully!")
                    flashed.add(drive_key)

                elif "RP2350" in label.upper():
                    print(f"\n[+] DETECTED MASTER BOOTSEL DRIVE on {letter}:\\ ({label})")
                    target = f"{letter}:\\ounce_master.uf2"
                    print(f"    --> Copying {MASTER_UF2} to {target}...")
                    shutil.copyfile(MASTER_UF2, target)
                    print(f"  [SUCCESS] Master RP2350 Flashed Successfully!")
                    flashed.add(drive_key)

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n[+] Auto-flasher stopped.")

if __name__ == '__main__':
    main()
