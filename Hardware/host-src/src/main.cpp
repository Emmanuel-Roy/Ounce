#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "pico/stdlib.h"
#include "spi_master.h"
#include "tusb.h"

#define MAX_CONSECUTIVE_UNACK 1000

uint16_t last_seen_count[4] = {0};
uint32_t frozen_count_streak[4] = {0};
absolute_time_t spi_pause_until = nil_time;

// Diagnostic-print replacement for printf()+fflush(stdout). printf() routes
// through pico_stdio's print_mutex and then stdio_usb's own stdio_usb_mutex,
// either of which can stall this loop (see the tud_cdc_read() comment below
// for why that mattered here). This writes directly to the TinyUSB CDC
// endpoint instead: best-effort, never blocks, and just drops the line if
// there isn't FIFO space for it rather than waiting.
static void cdc_printf(const char *fmt, ...) {
    if (!tud_cdc_connected()) return;

    char buf[160];
    va_list args;
    va_start(args, fmt);
    int len = vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    if (len <= 0) return;
    if (len > (int)sizeof(buf)) len = (int)sizeof(buf);

    uint32_t avail = tud_cdc_write_available();
    uint32_t n = (uint32_t)len;
    if (n > avail) n = avail;
    if (n > 0) {
        tud_cdc_write(buf, n);
        tud_cdc_write_flush();
    }
}

int main() {
    stdio_init_all();
	const uint HEARTBEAT_PIN = 25;          // on-board LED on most Pico 2 boards
	gpio_init(HEARTBEAT_PIN);
	gpio_set_dir(HEARTBEAT_PIN, GPIO_OUT);
    busy_wait_us(500 * 1000); // Allow USB CDC stack to connect to host PC.
                              // busy_wait, not sleep_ms - sleep_ms routes through
                              // sleep_until()/__wfe() (see spi_master.cpp), which
                              // could hang the firmware before it ever starts.
    cdc_printf("=== OUNCE PRIMARY RP2350 MASTER RUNNING ===\n");

    spi_master_init();

    ControllerSpiPacket packets[4];
    ControllerSpiAckPacket ack_packets[4];
    bool ack_status[4] = {false};
    uint32_t consecutive_unack_count[4] = {0};
    uint32_t last_serial_rx_time[4] = {0};

    auto reset_packet = [](ControllerSpiPacket &p, uint8_t id) {
        p.header = 0x5A;
        p.flags = id;          // target id only; Home/Capture cleared
        p.buttons = 0;
        p.lx = 128;
        p.ly = 128;
        p.rx = 128;
        p.ry = 128;
        p.crc8 = 0;
    };

    for (int i = 0; i < 4; i++) {
        reset_packet(packets[i], static_cast<uint8_t>(i));
        memset(&ack_packets[i], 0, sizeof(ControllerSpiAckPacket));
    }

    uint8_t serial_buf[9];
    size_t serial_idx = 0;
    uint32_t last_byte_time = 0;
    uint32_t loop_count = 0;
    absolute_time_t next_frame = get_absolute_time();

    // Loop timing instrumentation. The loop was measured running at ~847Hz
    // against a 500us target, i.e. a ~1.18ms body, and the known costs (CS
    // setup/hold, the transfer, the bounded drain) only account for ~330us.
    // These accumulators attribute the rest to a specific phase.
    uint32_t prof_usb = 0, prof_spi = 0, prof_body = 0, prof_max = 0, prof_n = 0;
    uint32_t body_start = time_us_32();

    while (true) {
        {
            uint32_t body = time_us_32() - body_start;
            if (prof_n) {                      // skip the first, partial pass
                prof_body += body;
                if (body > prof_max) prof_max = body;
            }
            body_start = time_us_32();
        }

        // Service TinyUSB ourselves, once per loop, as the ONLY caller.
        // PICO_STDIO_USB_ENABLE_IRQ_BACKGROUND_TASK is disabled (see
        // CMakeLists.txt) specifically so nothing else ever calls tud_task()
        // from an IRQ. Both tud_task() and the tud_cdc_*() calls below claim
        // TinyUSB's internal _usbd_mutex (a blocking mutex); mixing an IRQ
        // context with mainline on that mutex is a real deadlock hazard (the
        // SDK's own mutex.h warns against blocking mutex calls from an IRQ
        // handler) and was the actual cause of this firmware's intermittent
        // full hangs. Single-context polling removes the race entirely.
        uint32_t t_mark = time_us_32();
        tud_task();
        prof_usb += time_us_32() - t_mark;

        uint32_t now = to_ms_since_boot(get_absolute_time());

        if (serial_idx > 0 && (now - last_byte_time > 20)) {
            serial_idx = 0;
        }

        // Read host input straight from the TinyUSB CDC FIFO (see comment
        // above - this and cdc_printf() are the only other tud_cdc_*() call
        // sites, and they're safe now that tud_task() only ever runs here
        // too).
        uint32_t rx_avail = tud_cdc_available();
        if (rx_avail) {
            uint8_t rx_chunk[64];
            if (rx_avail > sizeof(rx_chunk)) rx_avail = sizeof(rx_chunk);
            uint32_t rx_count = tud_cdc_read(rx_chunk, rx_avail);
            for (uint32_t k = 0; k < rx_count; k++) {
                uint8_t byte = rx_chunk[k];
                last_byte_time = now;

                if (serial_idx == 0) {
                    if (byte == 0x5A) {
                        serial_buf[0] = byte;
                        serial_idx = 1;
                    }
                } else {
                    serial_buf[serial_idx++] = byte;
                    if (serial_idx == 9) {
                        // Low 2 bits select the target; the rest of the byte
                        // carries Home/Capture and rides through untouched.
                        uint8_t target_id = serial_buf[1] & SPI_TARGET_ID_MASK;
                        uint8_t expected_crc = calculate_crc8(serial_buf, 8);
                        if (serial_buf[8] == expected_crc) {
                            packets[target_id].flags = serial_buf[1];
                            memcpy(&packets[target_id].buttons, &serial_buf[2], 2);
                            packets[target_id].lx = serial_buf[4];
                            packets[target_id].ly = serial_buf[5];
                            packets[target_id].rx = serial_buf[6];
                            packets[target_id].ry = serial_buf[7];
                            last_serial_rx_time[target_id] = now;
                        }
                        serial_idx = 0;
                    }
                }
            }
        }

        // Neutralize stale serial targets
		for (int i = 0; i < 4; i++) {
			if (last_serial_rx_time[i] == 0 || (now - last_serial_rx_time[i] > 200)) {
				reset_packet(packets[i], static_cast<uint8_t>(i));
			}
		}

		// Pause SPI so Slave can hard-reset with an idle bus
		if (!time_reached(spi_pause_until)) {
			// bus idle
		} else {
			for (int i = 0; i < 1; i++) { // ONLY Target 0
				packets[i].crc8 = calculate_crc8(reinterpret_cast<const uint8_t*>(&packets[i]), 8);
				uint32_t t_spi = time_us_32();
				ack_status[i] = spi_master_transceive_packet(i, packets[i], ack_packets[i]);
				prof_spi += time_us_32() - t_spi;

				if (ack_status[i]) {
					consecutive_unack_count[i] = 0;

					if (ack_packets[i].packet_count == last_seen_count[i]) {
						frozen_count_streak[i]++;
						if (frozen_count_streak[i] >= 20) {
							cdc_printf(" << RECOVER: Target %d frozen count=%u - pausing SPI 100ms\n",
								   i, ack_packets[i].packet_count);
							gpio_put(20, 1);
							spi_master_init();
							spi_pause_until = make_timeout_time_ms(100);
							frozen_count_streak[i] = 0;
							last_seen_count[i] = 0xFFFF;
						}
					} else {
						last_seen_count[i] = ack_packets[i].packet_count;
						frozen_count_streak[i] = 0;
					}
				} else {
					// No valid ACK at all – this is the path that was missing
					consecutive_unack_count[i]++;
					frozen_count_streak[i] = 0;
					if (consecutive_unack_count[i] >= 30) {
						cdc_printf(" << RECOVER: Target %d %u consecutive fails - pausing SPI 150ms\n",
							   i, consecutive_unack_count[i]);
						gpio_put(20, 1);
						spi_master_init();
						spi_pause_until = make_timeout_time_ms(150);
						consecutive_unack_count[i] = 0;
						last_seen_count[i] = 0xFFFF;
					}
				}
			}
		}

		loop_count++;

		// Report the timing breakdown roughly once a second. "body" is the
		// whole iteration including the pacing wait, so body minus usb minus
		// spi is everything else (CDC reads, the neutralize scan, printing,
		// and the busy_wait_until pacing itself).
		prof_n++;
		if (prof_n >= 1000) {
			cdc_printf(" << T body=%lu usb=%lu spi=%lu max=%lu\n",
				   (unsigned long)(prof_body / prof_n),
				   (unsigned long)(prof_usb / prof_n),
				   (unsigned long)(prof_spi / prof_n),
				   (unsigned long)prof_max);
			prof_body = prof_usb = prof_spi = prof_max = 0;
			prof_n = 0;
		}

		if (loop_count % 30 == 0) {
			for (int i = 0; i < 1; i++) {   // ONLY Target 0
				if (ack_status[i]) {
					cdc_printf(" << ACK: Target %d | btns=0x%04X | LX=%d LY=%d | count=%u\n",
						   i, packets[i].buttons, packets[i].lx, packets[i].ly,
						   ack_packets[i].packet_count);
				} else {
					// Kept under 64 bytes on purpose: that is the CDC TX FIFO
					// size, and cdc_printf() truncates anything longer - which
					// silently eats the trailing newline and makes consecutive
					// lines run together in the log.
					cdc_printf(" << DIAG T%d Hdr:%02X ID:%02X Cnt:%u CRC:%02X\n",
						   i, ack_packets[i].header, ack_packets[i].slave_id,
						   ack_packets[i].packet_count, ack_packets[i].crc8);
				}
			}
		}

        // 500us => 2kHz polling. The SPI burst itself is only ~18us at 4MHz,
        // so the poll period - not the clock - is what bounds how quickly a
        // new input reaches the target. Loop body is roughly 130us (110us of
        // CS setup/hold plus the transfer), leaving comfortable margin here.
        next_frame = delayed_by_us(next_frame, 500);

        // Never let the schedule fall behind real time. delayed_by_us() advances
        // by a fixed 1ms regardless of how long the iteration actually took, so
        // after any stall next_frame sits in the past and the wait below returns
        // instantly for thousands of iterations - measured free-running at
        // 6000 Hz, six times normal, hammering the slave right when it is least
        // able to cope.
        if (absolute_time_diff_us(get_absolute_time(), next_frame) < 0) {
            next_frame = get_absolute_time();
        }

		gpio_put(HEARTBEAT_PIN, (loop_count >> 8) & 1);   // toggles ~every 256 ms

        // busy_wait_until(), NOT sleep_until(). sleep_until() parks the core in
        // __wfe() waiting for a timer alarm to fire an SEV; if that alarm event
        // is lost the core never wakes. It then stops servicing USB, so TinyUSB
        // leaves the bulk OUT endpoint unarmed and the USB hardware NAKs the
        // host in hardware without raising an interrupt - so no event is ever
        // generated to release the __wfe(). The halt sustains itself: measured
        // stopping the loop dead for 8s, and once for 4min24s, recoverable only
        // by a control transfer (a host DTR toggle hitting always-armed EP0).
        // That was the cause of the intermittent freezes. This loop has nothing
        // else to do with the spare cycles, so spinning here costs us nothing.
        busy_wait_until(next_frame);
    }

    return 0;
}
