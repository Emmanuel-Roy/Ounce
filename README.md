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
| `bridge.bat` | Launcher for the bridge. Double-click it, or add it to Steam so Steam Input presents a Steam Controller as a normal gamepad. |

### Driving the controllers

Run it with no arguments and it asks which input drives each virtual
controller:

```text
Detected inputs:
   1) PS5 Controller
   k) Keyboard
   d) Disabled

  Controller 1 (slot 0) : 1
  Controller 2 (slot 1) : k
  Controller 3 (slot 2) : d
  Controller 4 (slot 3) : k
```

Only real controllers are listed — the Ounce's own targets are filtered out,
since using one as an input would feed our own output back in. Prompting is
skipped automatically when input is not a terminal, so scripts never block.

Everything can also be driven from flags:

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

Outside Steam, a Steam Controller is a keyboard/mouse device — there is no
gamepad for anything to detect. Steam Input is what turns it into a virtual
gamepad, and Steam only does that for processes **it launches itself**. So:

1. **Steam → Games → Add a Non-Steam Game → Browse** → select `tools/bridge.bat`
2. Right-click it in your library → **Properties → Controller** →
   **Enable Steam Input**
3. **Properties → Controller → Edit Layout** → bind the sticks to
   **Joystick Move**, *not* D-Pad (see below)
4. Launch it from Steam — a console opens and the controller appears in the
   list like any other pad, so you can assign it to a slot as normal

**Bind sticks to Joystick Move.** Several of Steam's non-Steam-game templates
map a thumbstick to D-Pad, which reduces it to eight directions *before* the
bridge ever sees it. The analog range is gone at that point and nothing on this
side can recover it. To check what Steam is actually sending, set Launch
Options to `--probe` and watch whether stick motion moves the `AXES` values
(good) or the `HATS` values (stick is bound to D-Pad).

`bridge.bat` sets `SDL_JOYSTICK_HIDAPI_STEAM=0` deliberately. Left on, SDL
talks to the Steam Controller directly over HID and **bypasses Steam Input
entirely**, so the layout you configured would silently do nothing.

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
│   ├── bridge.bat                        <-- Launcher (works via Steam too)
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
