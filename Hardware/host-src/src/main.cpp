#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "spi_master.h"

#define MAX_CONSECUTIVE_UNACK 10

int main() {
    stdio_init_all();
    sleep_ms(500); // Allow USB CDC stack to connect to host PC
    printf("=== OUNCE PRIMARY RP2350 MASTER RUNNING ===\n");
    fflush(stdout);

    spi_master_init();

    ControllerSpiPacket packets[4];
    ControllerSpiAckPacket ack_packets[4];
    bool ack_status[4] = {false};
    uint32_t consecutive_unack_count[4] = {0};
    uint32_t last_serial_rx_time[4] = {0};

    auto reset_packet = [](ControllerSpiPacket &p, uint8_t id) {
        p.header = 0x5A;
        p.target_id = id;
        p.buttons = 0;
        p.lx = 128;
        p.ly = 128;
        p.rx = 128;
        p.crc8 = 0;
    };

    for (int i = 0; i < 4; i++) {
        reset_packet(packets[i], static_cast<uint8_t>(i));
        memset(&ack_packets[i], 0, sizeof(ControllerSpiAckPacket));
    }

    uint8_t serial_buf[8];
    size_t serial_idx = 0;
    uint32_t last_byte_time = 0;
    uint32_t loop_count = 0;
    absolute_time_t next_frame = get_absolute_time();

    while (true) {
        uint32_t now = to_ms_since_boot(get_absolute_time());

        if (serial_idx > 0 && (now - last_byte_time > 20)) {
            serial_idx = 0;
        }

        int c = getchar_timeout_us(0);
        while (c != PICO_ERROR_TIMEOUT) {
            uint8_t byte = static_cast<uint8_t>(c);
            last_byte_time = now;

            if (serial_idx == 0) {
                if (byte == 0x5A) {
                    serial_buf[0] = byte;
                    serial_idx = 1;
                }
            } else {
                serial_buf[serial_idx++] = byte;
                if (serial_idx == 8) {
                    uint8_t target_id = serial_buf[1];
                    if (target_id < 4) {
                        uint8_t expected_crc = calculate_crc8(serial_buf, 7);
                        if (serial_buf[7] == expected_crc) {
                            packets[target_id].target_id = target_id;
                            memcpy(&packets[target_id].buttons, &serial_buf[2], 2);
                            packets[target_id].lx = serial_buf[4];
                            packets[target_id].ly = serial_buf[5];
                            packets[target_id].rx = serial_buf[6];
                            last_serial_rx_time[target_id] = now;
                        }
                    }
                    serial_idx = 0;
                }
            }
            c = getchar_timeout_us(0);
        }

        for (int i = 0; i < 4; i++) {
            if (last_serial_rx_time[i] == 0 || (now - last_serial_rx_time[i] > 200)) {
                reset_packet(packets[i], static_cast<uint8_t>(i));
            }
        }

        for (int i = 0; i < 1; i++) {
            packets[i].crc8 = calculate_crc8(reinterpret_cast<const uint8_t*>(&packets[i]), 7);
            ack_status[i] = spi_master_transceive_packet(i, packets[i], ack_packets[i]);
            if (ack_status[i]) {
                consecutive_unack_count[i] = 0;
            } else {
                consecutive_unack_count[i]++;
                if (consecutive_unack_count[i] >= MAX_CONSECUTIVE_UNACK) {
                    consecutive_unack_count[i] = 0;
                    spi_master_init();
                }
            }
        }

        loop_count++;
        if (loop_count % 30 == 0) {
            for (int i = 0; i < 1; i++) {
                if (ack_status[i]) {
                    printf("  << ACK: Target %d | btns=0x%04X | LX=%d LY=%d | count=%u\n",
                           i, packets[i].buttons, packets[i].lx, packets[i].ly, ack_packets[i].packet_count);
                } else {
                    printf("  << MISO DIAG: Target %d | Hdr: 0x%02X | ID: 0x%02X | Count: %u | CRC: 0x%02X\n",
                           i, ack_packets[i].header, ack_packets[i].slave_id, ack_packets[i].packet_count, ack_packets[i].crc8);
                }
                fflush(stdout);
            }
        }

        next_frame = delayed_by_us(next_frame, 1000);
        sleep_until(next_frame);
    }

    return 0;
}
