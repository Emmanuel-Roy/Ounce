# Ounce

Play your Nintendo Switch from anywhere. Four Picos each appear to the Switch as
a real Switch Pro Controller, a fifth drives them over SPI, and a Windows client
turns your keyboard and USB pads into those four players while showing the
Switch's video in the same window.

```
keyboard / pads -> Windows client -> USB serial -> Pico 2 W master
                                                        |  SPI
                                          +------+------+------+------+
                                        Pico   Pico   Pico   Pico   -> Switch USB
                                       player1 player2 player3 player4
```

## Parts

| Qty | Part | Purpose |
| --- | --- | --- |
| 1 | **Raspberry Pi Pico 2 W** (RP2350) | Master |
| 4 | **Raspberry Pi Pico 1** (RP2040) | Servants — one per player |
| 4 | Micro-USB cables | Servants -> Switch (powered hub into the dock) |
| 1 | Micro-USB / USB-C cable | Master -> PC (power + serial) |
| 1 | **Elgato capture card** (4K S or similar) | Switch video in the client window |
| 1 | **Ounce PCB** or breadboard + jumpers | SPI bus and common ground |

Capture card is optional — run with `--no-preview` for input only.

## Layout

```
bin/               Flash these: ounce_master.uf2, GP2040-CE_0.0.0_Pico.uf2, OunceBridge/
Ounce-Client/      Windows client: test_bridge.py, wiring_test.py, bridge.bat, build_exe.bat
Ounce-Hardware/    master-firmware/, servant-firmware/, pcb/, pico-sdk/
```

## Wiring

SCK and MOSI are **shared** by all four servants. CS and MISO are **one per
servant** — an RP2040 slave does not release MISO when deselected, so four on
one wire would fight.

**Master (Pico 2 W)**

| Signal | GPIO | Pin | To |
| --- | --- | --- | --- |
| SCK | GP18 | 24 | SCK on all servants |
| MOSI | GP19 | 25 | MOSI (GP16) on all servants |
| CS 0–3 | GP21, GP22, GP26, GP27 | 27, 29, 31, 32 | CS on servant 0, 1, 2, 3 |
| MISO 0–3 | GP0, GP4, GP16, GP20 | 1, 6, 21, 26 | MISO from servant 0, 1, 2, 3 |
| GND | GND | 3, 8, 13, 18, 23, 28, 33, 38 | GND on every servant |

**Each servant (Pico 1)** — identical for all four:

| Signal | GPIO | Pin | To |
| --- | --- | --- | --- |
| MOSI in | GP16 | 21 | master GP19 |
| CS | GP17 | 22 | that servant's own CS pin |
| SCK | GP18 | 24 | master GP18 |
| MISO out | GP19 | 25 | that servant's own MISO pin |
| GND | GND | 23, 38 | master GND |

**Every board must share a ground with the master** — without it you get
intermittent garbage rather than a clean failure. Bus is 4 MHz, SPI mode 1.
Servants are numbered purely by which CS pin they are wired to, so any Pico
works in any slot.

Verify before going further:

```bash
python Ounce-Client/wiring_test.py            # PASS/FAIL per slot
python Ounce-Client/wiring_test.py --identify # drive one slot at a time
python Ounce-Client/wiring_test.py --map      # slot -> board -> Windows USB device
```

SPI slot order and USB enumeration order are unrelated — slot 2 is just "the
board on CS GP26" and may appear third or fourth in Windows.

## Flashing

Prebuilt images are in `bin/` — no build needed.

| Board | Hold BOOTSEL, plug in, drive appears | Drag on |
| --- | --- | --- |
| Servants (×4) | `RPI-RP2` | `bin/GP2040-CE_0.0.0_Pico.uf2` |
| Master | `RP2350` | `bin/ounce_master.uf2` |

All four servants get the **same** file; each learns its player number from its
CS pin.

## Building

```bash
git clone --recursive https://github.com/Emmanuel-Roy/Ounce.git
```

Already cloned? `git submodule update --init --recursive`.

**Master firmware** — needs CMake, Ninja and `arm-none-eabi-gcc` (the [Pico
Windows installer](https://www.raspberrypi.com/documentation/microcontrollers/c_sdk.html)
has all three):

```bash
cd Ounce-Hardware/master-firmware
cmake -B build -DPICO_SDK_PATH=../pico-sdk
cmake --build build
```

Add `-G Ninja` or `-G "MinGW Makefiles"` if CMake does not pick a generator on
its own. Output is `build/ounce_master.uf2`.

**Servant firmware** (`SKIP_WEBBUILD` avoids needing Node/npm for GP2040-CE's
unused web configurator):

```bash
cd Ounce-Hardware/servant-firmware/GP2040-CE-SPI
cmake -B build -DSKIP_WEBBUILD=ON
cmake --build build
```

**Client:**

```bash
pip install -r Ounce-Client/requirements.txt
python Ounce-Client/test_bridge.py
```

`python-vlc` is only the binding — also install VLC itself, at the same
bit-width as your Python, or video will not start.

`Ounce-Client\build_exe.bat` rebuilds `bin\OunceBridge\`. It uses `--onedir`
deliberately: a onefile build relaunches itself as a child process, and Steam
Input only instruments the process Steam launched. Keep the folder together —
the exe needs `_internal\` beside it.

## Running

Run with no arguments and it asks what drives each player:

```text
   1) PS5 Controller     k) Keyboard     d) Disabled

  Controller 1 (slot 0) : 1
  Controller 2 (slot 1) : k
```

Ounce's own servants are filtered out of the list. Only assigned slots are
enabled. To skip the prompt:

```bash
python test_bridge.py --assign all=keyboard
python test_bridge.py --assign 0=pad:DualSense --assign 1,2,3=keyboard
python test_bridge.py --list-controllers
```

`SLOT` is `0`–`3`, a comma list, or `all`; `SOURCE` is `keyboard` or
`pad:<index|name>` (prefer names — indices shift when devices are replugged).

On the Switch: **System Settings → Controllers and Sensors → Pro Controller
Wired Communication → ON**.

**Video** — `--window` for 4K60, `--list-modes` to see what your card offers,
`--capture-mode 1920x1080@240` to pick one. **F11** toggles borderless
fullscreen. A mode must be requested explicitly or DirectShow hands out 640×480;
4K60 and the other high modes are MJPEG only, since raw 4K60 will not fit over
USB. The card's HDMI passthrough never reaches the PC, so only the capture path
can feed the window.

## Steam Controller (Steam Input)

Outside Steam a Steam Controller is a keyboard/mouse device. Steam Input turns
it into a gamepad, but only for processes **Steam launches itself**:

1. **Steam → Games → Add a Non-Steam Game → Browse** →
   `bin\OunceBridge\OunceBridge.exe`
2. Right-click → **Properties → Controller → Enable Steam Input**
3. **Edit Layout** → bind sticks to **Joystick Move**, *not* D-Pad
4. Launch from Steam, then **click the window once** — Steam only leaves
   Desktop mode for a focused window, or the pad stays in mouse mode

Sticks bound to D-Pad are cut to eight directions before Ounce sees them and the
analog range cannot be recovered. Set Launch Options to `--probe` to check:
motion should move `AXES`, not `HATS`.

`bridge.bat` sets `SDL_JOYSTICK_HIDAPI_STEAM=0` on purpose — left on, SDL grabs
the pad over HID and bypasses Steam Input entirely.

## Remapping controls

Dropdown at the top of the window → **Remap keyboard controls…**. Click an
input, press a key. **Esc** cancels, **reset all** restores defaults. Rebinding
a key in use takes it from its previous owner, and changes apply instantly to
every player on the keyboard.

| Input | Key | Input | Key | Input | Key |
| --- | --- | --- | --- | --- | --- |
| L-Stick U/D/L/R | `W` `S` `A` `D` | D-Pad | arrows | X / Y / B / A | `I` `J` `K` `L` |
| R-Stick U/D/L/R | `8` `0` `7` `9` | L / R | `U` `O` | ZL / ZR | `Y` `P` |
| L3 / R3 | `Z` `X` | + / − | `N` `M` | Home / Capture | `H` `C` |

The same dropdown holds the video device, capture mode (raw vs compressed), HDR
toggle, and each player's input source. For pad mappings or a saved keyboard
layout:

```bash
python test_bridge.py --dump-config mymap.json
python test_bridge.py --config mymap.json
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| One slot fails wiring test | That slot's CS or MISO — SCK/MOSI faults break *all* slots |
| All slots fail | Missing common ground, or SCK/MOSI disconnected |
| Enumerates on Switch but does nothing | Wired communication off in Switch settings |
| Steam Controller acts as a mouse | Window not focused, or Steam Input not enabled |
| Sticks give only 8 directions | Steam layout has them on D-Pad |
| Video is 640×480 | No capture mode requested |
| Video will not start | VLC missing, or wrong bit-width for your Python |
