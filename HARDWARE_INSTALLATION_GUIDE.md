# Ounce: Hardware Installation & Toolchain Setup Guide

This guide covers hardware wiring (supporting up to 8 controllers), toolchain installation, building firmware binaries for both the **RP2350 Master** and **RP2040 Slaves**, physical board flashing, and Nintendo Switch deployment.

---

## 1. Hardware & Wiring Setup

### Required Components

* **1x Master Board**: Raspberry Pi Pico 2 (RP2350)
* **4x Slave Boards** (Active Setup): Raspberry Pi Pico 1 (RP2040) *(Expandable up to 8 Slaves)*
* **4x Micro-USB to USB-A Cables**: For connecting Slaves to the Nintendo Switch or PC
* **1x Micro-USB / USB-C Cable**: For Master power & serial connection to Host PC
* **Jumper Wires & Breadboard**: For SPI bus and Common Ground connections

### Pinout & Bus Connections (Up to 8 Slaves)

| Signal | Master (Pico 2 / RP2350) | Target Board | Connection Status | Bus Type |
| :--- | :--- | :--- | :--- | :--- |
| **GND** | **GND** (Any GND pin) | **All Slaves** | **Active (Mandatory)** | **Common Ground** |
| **SCK** | **GP18** (Pin 24) | **All Slaves** | **Active (Shared)** | Shared (`spi0_sclk`) |
| **MOSI** | **GP19** (Pin 25, `spi0_tx`) | **All Slaves** | **Active (Shared)** | Shared (`spi0_rx`) |
| **CS 0** | **GP20** (Pin 26) | **Slave 0** | **Active Connected** | Dedicated Chip Select |
| **CS 1** | **GP21** (Pin 27) | **Slave 1** | **Active Connected** | Dedicated Chip Select |
| **CS 2** | **GP22** (Pin 29) | **Slave 2** | **Active Connected** | Dedicated Chip Select |
| **CS 3** | **GP26** (Pin 31) | **Slave 3** | **Active Connected** | Dedicated Chip Select |
| **CS 4** | **GP27** (Pin 32) | **Slave 4** | Theoretical Expansion | Reserved CS 4 |
| **CS 5** | **GP28** (Pin 34) | **Slave 5** | Theoretical Expansion | Reserved CS 5 |
| **CS 6** | **GP14** (Pin 19) | **Slave 6** | Theoretical Expansion | Reserved CS 6 |
| **CS 7** | **GP15** (Pin 20) | **Slave 7** | Theoretical Expansion | Reserved CS 7 |

> [!CAUTION]
> **Common Ground**: All connected boards MUST share a common Ground connection to ensure SPI signal integrity at $10\text{ MHz}$.

---

## 2. Software Prerequisites & Toolchain Setup

### A. Windows Toolchain Installation

1. **Raspberry Pi Pico Windows Installer (Recommended)**:
   Download and run the official installer from [raspberrypi.com/documentation/microcontrollers/c_sdk.html](https://www.raspberrypi.com/documentation/microcontrollers/c_sdk.html).
   This automatically installs:
   - CMake ($\ge 3.13$)
   - GNU Arm Embedded Toolchain (`arm-none-eabi-gcc`)
   - Ninja / MinGW build tools
   - Python 3.8+
   - Git for Windows

2. **Python Dependencies**:
   Install `pyserial` and `pynput` for the Keyboard Test Bridge:
   ```cmd
   pip install pyserial pynput
   ```

### B. Repository Checkout

Clone the repository recursively to fetch all GP2040-CE submodules (`tinyusb`, `pico_pio_usb`):

```bash
git clone --recursive git@github.com:Emmanuel-Roy/Ounce.git
cd Ounce
```

---

## 3. Building & Flashing Firmware Binaries

### Step 1: Build & Flash Slave Firmware (RP2040 / Pico 1)

Each of the RP2040 Slave boards runs the modified GP2040-CE SPI input firmware.

```bash
cd Hardware/servant-src/GP2040-CE-SPI
mkdir build && cd build
cmake -DPICO_BOARD=pico ..
make -j4
```

1. Hold the **BOOTSEL** button on Slave Board 0 while plugging it into your PC via USB.
2. A USB drive named `RPI-RP2` will appear.
3. Drag and drop the compiled `GP2040-CE_v0.7.8_Pico.uf2` file onto the drive.
4. Repeat for your remaining connected Slave boards.

---

### Step 2: Build & Flash Master Firmware (RP2350 / Pico 2)

The Master board runs the $1\text{ kHz}$ SPI dispatcher (configured for `MAX_SLAVES = 8`, `ACTIVE_SLAVES = 4`) and USB Serial interface.

```bash
cd Hardware/host-src
mkdir build && cd build
cmake -DPICO_BOARD=pico2 ..
make -j4
```

1. Hold the **BOOTSEL** button on the Pico 2 Master board while plugging it into your PC.
2. Drag and drop `ounce_master.uf2` onto the `RP2350` drive.

---

## 4. Deployment to Nintendo Switch

1. Plug your connected Slave Pico boards into a powered USB Hub connected to the Nintendo Switch Dock.
2. Turn on your Nintendo Switch and navigate to:
   `System Settings` $\rightarrow$ `Controllers and Sensors` $\rightarrow$ `Pro Controller Wired Communication` $\rightarrow$ **ON**.
3. All connected virtual controllers will register automatically as Players 1 through $N$.
