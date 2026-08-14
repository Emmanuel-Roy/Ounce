# Ounce — Intermittent Freeze: ROOT CAUSE FOUND

Status: **root cause identified and empirically proven on hardware** (2026-08-14).
Baseline analysed and tested: `38d6ea5`, unmodified.

---

## TL;DR

**The master's CPU halts, parked in an ARM `__wfe()` (Wait-For-Event) instruction reached
via the SDK's `sleep_*()` family, because the timer alarm that was supposed to wake it is
lost. Nothing wakes it — the halt is indefinite. A USB *control* transfer is the only
thing that generates a wake event, which is why toggling DTR revives it instantly.**

Everything the user sees — dark slave LED, inputs stuck at default, the bridge hanging,
the burst of output on recovery — is a downstream consequence of the master CPU being
stopped.

**The fix is to remove every `sleep_*()` call from the firmware**, replacing them with the
`busy_wait_*()` equivalents, which spin on the timer and never execute `__wfe()`.

There are **four** such parking sites per loop iteration, not one:

| Site | File | Rate |
|---|---|---|
| `sleep_until(next_frame)` | `main.cpp` loop tail | 1 kHz |
| `sleep_us(30)` ×2, `sleep_us(50)` | `spi_master.cpp` `spi_master_transceive_packet()` | **3 kHz** |
| `sleep_ms(500)` | `main.cpp` boot | once |

`sleep_us()` and `sleep_ms()` both route through `sleep_until()`. **Fixing only the loop
tail is not sufficient and was empirically confirmed insufficient** — the freeze persisted,
because the three SPI waits carry 75% of the exposure. Verify a build with:

```sh
grep -oE "bl\s+[0-9a-f]+ <(sleep_until|sleep_us|sleep_ms|best_effort_wfe_or_timeout)>" ounce_master.dis
```

which must return **nothing**.

---

## Proof

Measured with a non-blocking probe that never calls `readline()` or `flush()`, so it
cannot itself stall. It sends a packet every 20 ms and logs every ACK's slave-side packet
counter, giving a direct measurement of how many main-loop iterations the master executed.

Across one fault, with DTR deliberately left alone for 8 seconds:

```
last ACK before fault : t=33.156   count=45832
first ACK after fault : t=41.328   count=45862
wall-clock gap        : 8.172 s
slave counter advanced: 30            <-- expected ~8172 if the loop were running
master executed 0.4% of expected iterations
implied loop rate during fault: 3.7 loops/s

rate 2 s before fault :  996.2 loops/s   (normal)
rate 2 s after  fault : 6061.4 loops/s   (catch-up burst)
```

The slave's counter increments once per master poll, so it is a direct tachometer for the
master's loop. **During the fault the master executes ~30 more iterations and then stops
completely.** It is not slowed, not muted, not merely failing SPI — it is halted.

Supporting measurements:

- **Both USB directions die together.** Host→device writes begin timing out at the same
  instant device→host output stops, and stay failing for the entire fault (verified over a
  20 s hold). A muted-but-running MCU would still drain writes. It does not.
- **The device never disconnects.** A port-enumeration watcher polling at 150 ms across
  400 s, covering four faults, recorded **zero** enumeration changes. The master is not
  rebooting or re-enumerating.
- **Re-asserting DTR revives it in 47–63 ms**, every time, in both directions
  simultaneously (5/5 faults).
- **Without a DTR toggle the halt is indefinite** — an early run left it dead for
  **4 minutes 24 seconds** with no recovery, ending only when the run was killed.
- **Frequency**: 7 faults in 540 s, mean interval 77 s, intervals ranging 6.4 s – 135 s —
  random, not a timer.
- Normal operation between faults is metronomically perfect: **exactly 1000.0 loops/s**
  and zero lost SPI packets (the counter advances by exactly 30 between prints). **The SPI
  link itself is not the problem.**

## Mechanism

`main.cpp:201` ends every iteration with `sleep_until(next_frame)`. In the SDK
(`pico-sdk/src/common/pico_time/time.c`):

```c
if (add_alarm_at(t_before, sleep_until_callback, NULL, false) >= 0) {
    while (!time_reached(t_before)) {
        uint32_t save = spin_lock_blocking(sleep_notifier.spin_lock);
        lock_internal_spin_unlock_with_wait(&sleep_notifier, save);
    }
}
```

and (`pico-sdk/src/common/pico_sync/include/pico/lock_core.h:140`):

```c
#define lock_internal_spin_unlock_with_wait(lock, save) spin_unlock((lock)->spin_lock, save), __wfe()
```

So the core parks in `__wfe()` and only continues when *some* event arrives. It is woken by
the alarm IRQ doing an SEV. If that alarm event is lost, the core sleeps until any other
interrupt happens.

**Why nothing else wakes it:** once parked, the master stops servicing USB. TinyUSB's bulk
OUT endpoint is left unarmed, so the USB hardware NAKs the host's writes *automatically, in
hardware, without raising an interrupt*. No interrupt means no event, which means the
`__wfe()` never returns. The system is self-sustaining: the halt prevents the very
interrupts that would end it.

**Why DTR specifically fixes it:** setting DTR makes Windows issue a
`SET_CONTROL_LINE_STATE` **control** transfer. Control transfers go to endpoint 0, which is
always armed, so the USB peripheral *does* raise an interrupt — generating the event that
releases `__wfe()`. The loop then resumes exactly where it left off. Ordinary bulk writes
cannot do this, which is precisely why the bridge's continuous 50 Hz writes never revive it.

This also explains why the user perceives the fault as self-recovering: `test_bridge.py`
defaults to `--relaunch-seconds 5.0`, which pulses DTR every 5 seconds. That pulse is
accidentally acting as the watchdog that un-sticks the master. All my test runs used
`--relaunch-seconds 0`, which removed the rescue and exposed the true, indefinite halt.

## How every symptom follows

| Symptom | Explanation |
|---|---|
| Slave LED goes dark | Master halted → no SPI → slave passes its 250 ms staleness threshold |
| Inputs stuck at default | Same staleness branch calls `reset_to_neutral()` |
| Bridge appears to hang | Device answers nothing; `ser.write`/`flush` stall, `readline` times out |
| Burst of output on recovery | Host-side serial backlog flushes once the master resumes |
| "Recovers on its own" | The 5 s DTR relaunch pulse is silently rescuing it |
| Occasional extra-fast catch-up | `next_frame` drift (below) — the loop free-runs at up to 6000/s afterwards |

## Secondary bug (real, and worth fixing at the same time)

`main.cpp:199` advances the schedule by a fixed step with no clamp:

```c
next_frame = delayed_by_us(next_frame, 1000);
```

After an 8 s halt, `next_frame` is 8 s in the past, so `sleep_until()` returns instantly for
thousands of iterations and the loop free-runs — **measured at 6061 loops/s, 6× normal** —
hammering the slave far faster than it was designed for right when it is least able to cope.
This is a genuine bug independent of the halt, and it is what produced the "everything
speeds up for a second" behaviour reported earlier.

## The fix

Applied in `main.cpp` (loop tail + boot delay) and `spi_master.cpp` (three per-transaction
waits). All `sleep_*()` calls become `busy_wait_*()`, which polls the timer directly and
never executes `__wfe()`. The loop has nothing else to do with the spare cycles, so
spinning costs nothing here.

A second, independent bug was fixed at the same time — `next_frame` is now clamped so it
cannot fall behind real time:

```c
        next_frame = delayed_by_us(next_frame, 1000);
        if (absolute_time_diff_us(get_absolute_time(), next_frame) < 0) {
            next_frame = get_absolute_time();
        }
```

Without this, after any stall `next_frame` sits in the past and the loop free-runs to
"catch up" — measured at **6061 loops/s, 6× normal** — hammering the slave exactly when it
is least able to cope.

### Staged evidence that the fix works

The clamp doubles as a way to confirm which firmware is actually flashed:

| Build | loop rate during fault | catch-up rate after fault |
|---|---|---|
| Baseline `38d6ea5` | 3.7 /s (0.4% of expected) | **6061 /s** |
| Loop tail only | 11 /s (1.1% of expected) | 1008 /s (clamp working, halt remains) |
| All four sites | *freeze no longer reproduces* | 1000 /s |

## What was ruled out (with evidence)

- **Master rebooting / brownout** — no USB re-enumeration across 400 s and 4 faults; and the
  boot banner never reappears.
- **SPI signal integrity / link desync** — between faults the link is *perfect*: exactly
  1000.0 loops/s and exactly 30 counter increments per printed line, i.e. zero dropped
  packets. The earlier "500 kHz reduces freeze frequency" observation is not reproduced by
  this data and was most likely confounded.
- **Slave (GP2040-CE) stalling** — input mode is hardcoded to `INPUT_MODE_SPI`
  (`gp2040.cpp:143-145`), taking a tight fast path (`gp2040.cpp:236-242`);
  `SwitchProDriver::process()` is `tud_hid_ready()`-gated and non-blocking; flash writes
  only occur on `saveRequested`. The slave's counter also proves it keeps up perfectly.
- **The old TinyUSB IRQ/mainline mutex deadlock** — verified compiled out:
  `PICO_STDIO_USB_ENABLE_IRQ_BACKGROUND_TASK=0` is present in
  `build/CMakeFiles/ounce_master.dir/flags.make` and no IRQ-task symbols remain in the map.
- **Host-side Python bugs** — real but secondary; see below. `py-spy` caught the bridge
  sitting at its normal loop sleep with ~31 ms CPU over 3.5 minutes, i.e. healthy and
  starved of data, not deadlocked.

## Still worth fixing afterwards (previously identified, all still valid)

These do not cause the freeze but they make it far worse to diagnose and to live with:

1. **`cdc_printf()` truncates lines instead of dropping them** (`main.cpp:31-33`). The CDC
   TX FIFO is 64 bytes (`CFG_TUD_CDC_TX_BUFSIZE`, full-speed). The `MISO DIAG` line is
   **74 bytes**, so its newline is always cut off; `ACK` (59 B) and `RECOVER` (63 B) fit.
   A line without a terminator makes `ser.readline()` block its full 50 ms timeout.
   Fix: drop the whole line when it will not fit, and shorten `MISO DIAG`.
2. **`ser.flush()` is an unbounded busy-wait** in pyserial on Windows
   (`while self.out_waiting: time.sleep(0.05)`, no timeout, `write_timeout` does not apply).
   Remove it; it is unnecessary after an 8-byte write.
3. **`ser.readline()` in the drain loop** should be a non-blocking buffered read.
4. **Master's RECOVER pauses SPI for 100/150 ms**, longer than the slave's 250 ms staleness
   budget can absorb comfortably; and `ack_status[]` is not cleared during a pause, so stale
   `MISO DIAG` lines are re-printed every 30 ms throughout it.
5. **`MAX_CONSECUTIVE_UNACK`** (`main.cpp:8`) is dead code.
6. **Packet structs are duplicated** in `host-src/include/packet.h` and
   `GP2040-CE-SPI/headers/drivers/spi/spiinputdriver.h`. They now carry a `static_assert`
   that the command and ACK packets are the same size (a synchronous SPI transfer clocks
   equal byte counts both ways), but nothing enforces that the two *files* agree — always
   flash master and slave as a matched pair.

### Correction: the "8 byte hard limit" was wrong

An earlier revision of this document asserted that packets could never exceed 8 bytes,
because the PL022 FIFOs are 8 entries deep. **That was an inference, never tested, and it
was wrong.** It came from misattributing this freeze to a 9-byte Home-button packet; the
freeze was in fact the `__wfe()` halt above, which is unrelated to packet size.

The packet is now **9 bytes** and works. The FIFO is 8 entries, but there is also a
transmit shift register, so 9 bytes fit. The master's `spi_transfer_with_timeout()` already
handled arbitrary lengths (it interleaves push and pop under
`rx_remaining < tx_remaining + fifo_depth`), and the slave drains RX continuously in its
tight loop. The only genuinely marginal spot was `fill_tx_fifo()` writing 9 bytes after a
single `TFE` test — if the shift register happened to be busy, the 9th write would be
silently dropped, truncating the CRC off every ACK. That now waits on `TNF` before each
write, with a bounded 50 µs deadline so it cannot hang when the master goes quiet.

## How to verify the fix

1. Apply the `busy_wait_until()` + clamp change, rebuild, flash the master only.
2. Run with the DTR rescue disabled so nothing can mask a halt:
   `python test_bridge.py --relaunch-seconds 0`
3. Run ≥15 minutes. Before the fix this produced ~7 halts in 9 minutes. After the fix
   there should be none.
4. Reproduction harnesses used for this investigation are in the job scratch dir
   (`probe.py`, `probe2.py`, `probe5.py`, `probe6.py`, `watch.py`) — `probe6.py` is the
   most useful, as it logs every ACK counter to CSV and measures the loop rate directly.
