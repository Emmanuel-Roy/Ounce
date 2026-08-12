#ifndef PACKET_H
#define PACKET_H

#include <cstdint>
#include <cstddef>

#pragma pack(push, 1)
// Unified 8-Byte Master -> Target SPI Packet Structure (Fits 8-entry RP2040 PL022 TX FIFO)
struct ControllerSpiPacket {
    uint8_t  header;    // Byte 0: Always 0x5A
    uint8_t  target_id; // Byte 1: Target ID (0..3)
    uint16_t buttons;   // Bytes 2..3: 16-bit button bitmask
    uint8_t  lx;        // Byte 4: Left Stick X (0..255, center 128)
    uint8_t  ly;        // Byte 5: Left Stick Y (0..255, center 128)
    uint8_t  rx;        // Byte 6: Right Stick X (0..255, center 128)
    uint8_t  crc8;      // Byte 7: Polynomial 0x07 over bytes 0..6
};

// Unified 8-Byte Target -> Master MISO ACK & Telemetry Packet Structure
struct ControllerSpiAckPacket {
    uint8_t  header;        // Byte 0: Always 0xA5 (ACK Header)
    uint8_t  slave_id;      // Byte 1: Target ID (0..3)
    uint8_t  status_flags;  // Byte 2: Bit 0 = Active, Bit 1 = Valid Packet Rx
    uint8_t  player_leds;   // Byte 3: Active Player LEDs (1..4)
    uint16_t packet_count;  // Bytes 4..5: Received Valid Packet Count
    uint8_t  reserved;      // Byte 6: 0x00
    uint8_t  crc8;          // Byte 7: Polynomial 0x07 over bytes 0..6
};
#pragma pack(pop)

// CRC-8 calculation with polynomial 0x07, initial value 0x00
inline uint8_t calculate_crc8(const uint8_t *data, size_t len) {
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; bit++) {
            if (crc & 0x80) {
                crc = (crc << 1) ^ 0x07;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

#endif // PACKET_H
