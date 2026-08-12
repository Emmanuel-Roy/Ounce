# Hardware

This directory contains hardware source files, KiCad PCB designs, firmware, host, and servant integration code for the Ounce project.

## Theoretical Scalability

The system firmware architecture is built to support up to **8 controllers** (`MAX_SLAVES = 8`), while the default physical setup uses **4 active boards** (`ACTIVE_SLAVES = 4`) to optimize hardware costs.

## Structure

- [`Ounce-PCB/`](./Ounce-PCB/): KiCad PCB schematic, layout, and project files for the Ounce carrier/breakout board.
- [`host-src/`](./host-src/): Host-side hardware source code and Master RP2350 SPI dispatcher (supports 8 CS lines).
- [`servant-src/`](./servant-src/): Embedded controller firmware and servant-side source code.
  - **Submodule / Forked Firmware**: [`GP2040-CE-SPI/`](./servant-src/GP2040-CE-SPI/) ([Emmanuel-Roy/GP2040-CE-SPI](https://github.com/Emmanuel-Roy/GP2040-CE-SPI))
  - **Upstream Project**: [OpenStickCommunity/GP2040-CE](https://github.com/OpenStickCommunity/GP2040-CE)
