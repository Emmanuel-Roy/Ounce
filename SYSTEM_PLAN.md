# GP2040-CE SPI Multi-Controller System Plan

This document outlines the complete architectural design and implementation plan for the **Ounce** multi-controller system.

---

## 1. System Overview & Scalability

The system is designed to theoretically support **up to 8 Slave boards** (8 independent controllers) driven by **1 Master board** (RP2350) over unidirectional SPI.

> [!NOTE]
> **Active Setup**: The physical hardware configuration uses **4 active Slave boards** (Slaves 0..3) to avoid purchasing extra hardware, while the firmware architecture (`MAX_SLAVES = 8`, `ACTIVE_SLAVES = 4`) supports scaling to 8 controllers.

```
+------------------------------------+
| Host PC (Testing: test_bridge.py)  |
+-----------------+------------------+
                  | USB Serial
                  v
+-----------------+------------------+
| Hardware/host-src (Pico 2 RP2350)  |
| 1 kHz SPI Master Dispatcher        |
| Config: MAX=8, ACTIVE=4            |
+-----------------+------------------+
                  | SCK, MOSI, CS0..CS7
       +----------+----------+----------+-----------------------+
       |          |          |          |                       |
       v          v          v          v                       v
   +-------+  +-------+  +-------+  +-------+             +-------------------+
   |Slave 0|  |Slave 1|  |Slave 2|  |Slave 3| ... [Expand] |Slaves 4-7 (Opt.)  |
   +---+---+  +---+---+  +---+---+  +---+---+             +---------+---------+
       |          |          |          |                           |
       +----------+----+-----+----------+---------------------------+
                       | USB HID
                       v
            [ Nintendo Switch / PC ]
```

### Folder Structure

```text
Ounce/
├── README.md
├── SYSTEM_PLAN.md                        <-- System Architecture Plan
├── INSTALLATION_GUIDE.md                 <-- Installation & Pinout Guide
├── .gitmodules
├── src/                                  <-- Top-level Application & Host Source
└── Hardware/
    ├── README.md
    ├── host-src/                         <-- RP2350 Master Firmware (Supports 8 controllers)
    │   ├── CMakeLists.txt
    │   ├── include/
    │   │   ├── packet.h
    │   │   └── spi_master.h
    │   ├── src/
    │   │   ├── main.cpp
    │   │   └── spi_master.cpp
    │   └── tools/
    │       └── test_bridge.py
    └── servant-src/
        ├── README.md
        └── GP2040-CE-SPI/                <-- RP2040 Slave Firmware (Submodule)
            ├── include/drivers/spiinputdriver.h
            ├── src/drivers/spiinputdriver.cpp
            └── src/managers/inputmanager.cpp
```

---

## 2. Hardware Pinout & CS Mapping (8-Controller System)

| Signal | Master (Pico 2 / RP2350) | Target Slave | Status | Bus Type |
| :--- | :--- | :--- | :--- | :--- |
| **GND** | **GND** (Any GND pin) | **All Slaves** | **Active** | **Common Ground** (Mandatory) |
| **SCK** | **GP18** (Pin 24) | **All Slaves** | **Active** | Shared (`spi0_sclk`) |
| **MOSI** | **GP19** (Pin 25, `spi0_tx`) | **All Slaves** | **Active** | Shared (Slave `spi0_rx`) |
| **CS 0** | **GP20** (Pin 26) | **Slave 0** | **Active** | Dedicated Chip Select |
| **CS 1** | **GP21** (Pin 27) | **Slave 1** | **Active** | Dedicated Chip Select |
| **CS 2** | **GP22** (Pin 29) | **Slave 2** | **Active** | Dedicated Chip Select |
| **CS 3** | **GP26** (Pin 31) | **Slave 3** | **Active** | Dedicated Chip Select |
| **CS 4** | **GP27** (Pin 32) | **Slave 4** | Theoretical Expansion | Dedicated Chip Select |
| **CS 5** | **GP28** (Pin 34) | **Slave 5** | Theoretical Expansion | Dedicated Chip Select |
| **CS 6** | **GP14** (Pin 19) | **Slave 6** | Theoretical Expansion | Dedicated Chip Select |
| **CS 7** | **GP15** (Pin 20) | **Slave 7** | Theoretical Expansion | Dedicated Chip Select |

---

## 3. Communication Protocol & Timing

### Packet Structure (11 Bytes Total, `#pragma pack(push, 1)`)

```cpp
#include <cstdint>

#pragma pack(push, 1)
struct ControllerSpiPacket {
    uint8_t  header;    // Always 0x5A
    uint16_t buttons;   // Bitmask for GP2040-CE logical buttons
    uint8_t  lx;        // Left Stick X (0..255, center 128)
    uint8_t  ly;        // Left Stick Y (0..255, center 128)
    uint8_t  rx;        // Right Stick X (0..255, center 128)
    uint8_t  ry;        // Right Stick Y (0..255, center 128)
    uint8_t  lt;        // Left Trigger (0..255)
    uint8_t  rt;        // Right Trigger (0..255)
    uint8_t  sequence;  // Frame counter (0..255)
    uint8_t  crc8;      // Polynomial 0x07 over bytes 0..9
};
#pragma pack(pop)
```

### 8-Controller Bus Timing Analysis
- **Single Packet Transmission**: $11\text{ bytes} \times 8\text{ bits/byte} / 10\text{ MHz} = \mathbf{8.8\ \mu\text{s}}$
- **Full 8-Slave Cycle**: $8 \times 8.8\ \mu\text{s} = \mathbf{70.4\ \mu\text{s}}$ total SPI active time.
- **Cycle Budget**: $70.4\ \mu\text{s}$ is only **7.04%** of the $1000\ \mu\text{s}$ ($1\text{ kHz}$) interval, proving 8-controller scaling is fully supported without timing jitter.

---

## 4. Slave Implementation (`Hardware/servant-src/GP2040-CE-SPI`)

1. **`include/drivers/spiinputdriver.h`**:
   - Class `SpiInputDriver` extending `BaseInputDriver`.
   - Hardware setup on `spi0` slave mode at 10 MHz.
   - 50 ms timeout watchdog timer for neutral fallback reset (`lx=128, ly=128, rx=128, ry=128, lt=0, rt=0, buttons=0`).

2. **`src/drivers/spiinputdriver.cpp`**:
   - Reads incoming bytes, verifies `0x5A` header, validates CRC-8.
   - Maps 8-bit analog inputs to GP2040-CE 16-bit range (`val * 257`).
   - Populates `gamepad->state`.

3. **`src/managers/inputmanager.cpp`**:
   - Instantiates `SpiInputDriver` and calls `initialize()` in `setup()`.
   - Invokes `spiInputDriver.poll(&(gamepad->state))` in `update()`.

---

## 5. Master Implementation (`Hardware/host-src`)

```cpp
constexpr int MAX_SLAVES = 8;
constexpr int ACTIVE_SLAVES = 4; // Currently wired physical boards
```

- Configured for Pico SDK with `pico2_arm` target.
- Initializes `spi0` Master @ 10 MHz and CS GPIO array `[20, 21, 22, 26, 27, 28, 14, 15]`.
- Main 1 kHz timer loop (`sleep_until` with 1000 µs interval) iterating over `ACTIVE_SLAVES`.
