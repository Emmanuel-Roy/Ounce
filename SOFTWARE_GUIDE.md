# Ounce: Software & Architecture Guide

This guide covers the software design, code organization, communication protocols, driver architecture, and execution flow of the **Ounce** multi-controller system.

---

## 1. Code Base Organization

```text
Ounce/
├── src/                                  <-- Top-level Host Software & Applications
└── Hardware/
    ├── host-src/                         <-- Master (RP2350) SPI Dispatcher & USB CDC Serial
    │   ├── CMakeLists.txt
    │   ├── include/
    │   │   ├── packet.h
    │   │   └── spi_master.h
    │   ├── src/
    │   │   ├── main.cpp
    │   │   └── spi_master.cpp
    │   └── tools/
    │       └── test_bridge.py            <-- Python Keyboard Test Bridge
    └── servant-src/
        └── GP2040-CE-SPI/                <-- Slave (RP2040) GP2040-CE Submodule
            ├── include/drivers/spiinputdriver.h
            ├── src/drivers/spiinputdriver.cpp
            └── src/managers/inputmanager.cpp
```

---

## 2. SPI Packet Protocol Layout

Both Master and Slave components share an identical 11-byte memory representation (`#pragma pack(push, 1)`):

```cpp
#include <cstdint>

#pragma pack(push, 1)
struct ControllerSpiPacket {
    uint8_t  header;    // Sync byte (0x5A)
    uint16_t buttons;   // Bitmask for GP2040-CE logical buttons
    uint8_t  lx;        // Left Stick X (0..255, center 128)
    uint8_t  ly;        // Left Stick Y (0..255, center 128)
    uint8_t  rx;        // Right Stick X (0..255, center 128)
    uint8_t  ry;        // Right Stick Y (0..255, center 128)
    uint8_t  lt;        // Left Trigger (0..255)
    uint8_t  rt;        // Right Trigger (0..255)
    uint8_t  sequence;  // Incremental frame counter
    uint8_t  crc8;      // CRC-8 (Polynomial 0x07 over bytes 0..9)
};
#pragma pack(pop)
```

---

## 3. Slave Driver Architecture (`SpiInputDriver`)

The RP2040 Slave firmware replaces default physical GPIO pin sampling by injecting inputs directly into GP2040-CE's `GamepadState`:

```
[ SPI0 RX (MOSI) ] ──► SpiInputDriver ──► GamepadState ──► Gamepad::process() ──► SwitchDriver ──► TinyUSB
```

* **Non-Blocking Parsing**: Validates `0x5A` header, verifies CRC-8, scales 8-bit analog inputs ($val \times 257 \rightarrow 0\dots 65535$).
* **Failsafe Watchdog**: If no valid packet arrives within $50\text{ ms}$, neutral fallback values are applied (`lx=128, ly=128, rx=128, ry=128, lt=0, rt=0, buttons=0`).

---

## 4. Master Dispatcher Loop (`Hardware/host-src`)

The RP2350 Master runs a high-precision $1\text{ kHz}$ dispatch loop:

1. Listens for host commands / test bridge frames over USB CDC Serial.
2. Updates local `ControllerSpiPacket` buffers for active controllers.
3. Iterates over active Slave CS lines (`CS 0` .. `CS 3`), asserting CS LOW, writing 11 bytes at $10\text{ MHz}$, and pulling CS HIGH.
4. Waits for the next $1000\ \mu\text{s}$ interval boundary via `sleep_until()`.

---

## 5. Running the Python Test Bridge

For software testing:

```bash
python Hardware/host-src/tools/test_bridge.py --port COM3
```

- **Controls**: `WASD` (Left Stick), `Arrows` (D-Pad), `K/J/I/U` (A/B/X/Y), `Q/E` (L/R), `1/2` (ZL/ZR).
- Connect Slave 0 to PC and verify live input visualization on [gamepad-tester.com](https://gamepad-tester.com/).
