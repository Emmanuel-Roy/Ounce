# Ounce: Play your Switch 2 Anywhere

Multi-controller proxy system using a **Raspberry Pi Pico 2 (RP2350) Master** board and **4x Raspberry Pi Pico 1 (RP2040) Slave** boards over $10\text{ MHz}$ SPI (theoretically expandable to 8 controllers).

## Documentation

- **[Hardware Installation & Wiring Guide](./HARDWARE_INSTALLATION_GUIDE.md)**: Hardware pinout, SPI bus connections, power, and physical board flashing.
- **[Software & Toolchain Guide](./SOFTWARE_GUIDE.md)**: C/C++ Pico SDK toolchain installation, CMake build commands, and Python test harness.
- **[System Architecture Plan](./SYSTEM_PLAN.md)**: System topology, 8-controller scaling, 11-byte packet memory layout, and CRC-8 specification.

## Tools

All host-side tools live in `tools/`.

| Tool | Purpose |
| --- | --- |
| `test_bridge.py` | The bridge. Reads a keyboard and/or physical controllers and drives up to 4 virtual Switch Pro Controllers. |
| `wiring_test.py` | Verifies the SPI wiring, identifies which physical board is which slot, and maps slots to USB devices. |
| `steam.bat` | Launches the bridge under Steam Input, so a Steam Controller can be configured through Steam's own layout editor. |

### Driving the controllers

```bash
# keyboard drives all four players
python test_bridge.py --assign all=keyboard

# player 1 on a DualSense, players 2-4 on the keyboard
python test_bridge.py --assign 0=pad:DualSense --assign 1,2,3=keyboard

# only players 2 and 4 active
python test_bridge.py --assign 1,3=keyboard

python test_bridge.py --list-controllers
```

`SLOT` is a number `0`–`3`, a comma list, or `all`. `SOURCE` is `keyboard` or
`pad:<index|name>` — prefer the name, since indices shift when devices are
plugged or unplugged. Assigning two sources to one slot merges them. Only the
slots you assign are enabled; the rest are disabled and never polled.

To change *which* keys or pad buttons map to which Switch input, per slot:

```bash
python test_bridge.py --dump-config mymap.json   # editable starting point
python test_bridge.py --config mymap.json
```

### Checking the wiring

```bash
python wiring_test.py              # PASS/FAIL per slot, with pins to check
python wiring_test.py --identify   # drive one slot at a time to see which board it is
python wiring_test.py --map        # slot -> physical board -> Windows USB device
```

`--map` is the one to reach for when you need to know which controller is
which: each slave reports its hardware unique ID over SPI *and* uses it as its
USB serial, so a slot resolves to a specific board and a specific device on the
PC. **SPI slot order and USB enumeration order are unrelated** — slot 2 is
simply "the board on CS GP26" and may well appear third or fourth in Windows.

### Steam Controller / Steam Input

Steam only applies Steam Input to processes **it launches itself**, so the
bridge has to be started from Steam — running it from a terminal gives you the
raw controller with no Steam remapping.

1. **Steam → Games → Add a Non-Steam Game → Browse** → select `tools/steam.bat`
2. Right-click it in your library → **Properties → Controller** →
   set *Override* to **Enable Steam Input**
3. **Properties → Controller → Edit Layout** → configure the Steam Controller
   however you like (this is the whole point — Steam does the remapping, the
   bridge just receives the result)
4. Optionally set **Launch Options**, which are forwarded to `test_bridge.py`:
   ```
   --assign 0=pad --assign 1,2,3=keyboard
   ```
5. Launch it from Steam

That default drives player 1 from the Steam Controller and players 2–4 from the
keyboard. `--assign 0=pad` with no name means "first usable controller", which
is what you want here since Steam's virtual pad has an unpredictable name.

`steam.bat` sets `SDL_JOYSTICK_HIDAPI_STEAM=0` deliberately. Left on, SDL talks
to the Steam Controller directly over HID and **bypasses Steam Input entirely**,
so the layout you configured would silently do nothing.

## Directory Structure

```text
Ounce/
├── README.md
├── SYSTEM_PLAN.md
├── HARDWARE_INSTALLATION_GUIDE.md
├── SOFTWARE_GUIDE.md
├── .gitmodules
├── bin/                                  <-- Prebuilt .uf2 images to flash
├── tools/                                <-- Host-side tools (see Tools above)
│   ├── test_bridge.py                    <-- The bridge
│   ├── wiring_test.py                    <-- Wiring / slot-mapping diagnostics
│   ├── steam.bat                         <-- Launch under Steam Input
│   ├── auto_flasher.py
│   └── auto_debugger.py
├── src/                                  <-- Top-level Application & Host Source
└── Hardware/
    ├── README.md
    ├── host-src/                         <-- RP2350 Master Firmware Source
    └── servant-src/
        ├── README.md
        └── GP2040-CE-SPI/                <-- RP2040 Slave Firmware (Submodule)
```

## Flashing

`bin/` holds the current images. Flash **`ounce_master.uf2`** to the RP2350
master and **`GP2040-CE_0.0.0_Pico.uf2`** to every RP2040 slave — it is a
single image for all four, each board learns its slot from the CS pin it is
wired to, so any Pico works in any slot with no per-board build.
