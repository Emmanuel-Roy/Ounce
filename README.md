# Ounce: Play your Switch 2 Anywhere

Multi-controller proxy system using a **Raspberry Pi Pico 2 (RP2350) Master** board and **4x Raspberry Pi Pico 1 (RP2040) Slave** boards over $10\text{ MHz}$ SPI (theoretically expandable to 8 controllers).

## Documentation

- **[Hardware Installation & Wiring Guide](./HARDWARE_INSTALLATION_GUIDE.md)**: Hardware pinout, SPI bus connections, power, and physical board flashing.
- **[Software & Toolchain Guide](./SOFTWARE_GUIDE.md)**: C/C++ Pico SDK toolchain installation, CMake build commands, and Python test harness.
- **[System Architecture Plan](./SYSTEM_PLAN.md)**: System topology, 8-controller scaling, 11-byte packet memory layout, and CRC-8 specification.

## Directory Structure

```text
Ounce/
├── README.md
├── SYSTEM_PLAN.md
├── HARDWARE_INSTALLATION_GUIDE.md
├── SOFTWARE_GUIDE.md
├── .gitmodules
├── src/                                  <-- Top-level Application & Host Source
└── Hardware/
    ├── README.md
    ├── host-src/                         <-- RP2350 Master Firmware Source
    └── servant-src/
        ├── README.md
        └── GP2040-CE-SPI/                <-- RP2040 Slave Firmware (Submodule)
```
