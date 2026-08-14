#ifndef PACKET_H
#define PACKET_H

#include <cstdint>
#include <cstddef>

// Contents of ControllerSpiPacket.flags. The target id only needs 2 bits, so
// the Switch Pro's two remaining buttons ride in the top of the same byte.
//
// Why this packet is 9 bytes and not 8: four 8-bit axes (32 bits) plus 18
// buttons is 50 bits of payload, but 8 bytes leaves only 48 after the header
// and CRC - short by 2 bits even with no target id at all. The only ways to
// fit 8 bytes are to cut analog resolution or to send some inputs less often
// than others, and both are ruled out: every input must keep full 8-bit range
// and every input must update at the same rate as every other. So the packet
// is 9 bytes and carries the complete controller state every single poll.
#define SPI_TARGET_ID_MASK   0x03
#define SPI_AUX_MASK_HOME    (1 << 6)
#define SPI_AUX_MASK_CAPTURE (1 << 7)

#pragma pack(push, 1)
// Unified 9-Byte Master -> Target SPI Packet Structure.
// Carries the complete Switch Pro Controller state every poll: 18 buttons (16
// here plus Home/Capture in flags) and all four analog axes at full 8-bit
// range. Nothing is multiplexed, so no input updates faster than another.
struct ControllerSpiPacket {
    uint8_t  header;    // Byte 0: Always 0x5A
    uint8_t  flags;     // Byte 1: [1:0] Target ID, [6] Home, [7] Capture
    uint16_t buttons;   // Bytes 2..3: 16-bit button bitmask
    uint8_t  lx;        // Byte 4: Left Stick X (0..255, center 128)
    uint8_t  ly;        // Byte 5: Left Stick Y (0..255, center 128)
    uint8_t  rx;        // Byte 6: Right Stick X (0..255, center 128)
    uint8_t  ry;        // Byte 7: Right Stick Y (0..255, center 128)
    uint8_t  crc8;      // Byte 8: Polynomial 0x07 over bytes 0..7
};

// Unified 9-Byte Target -> Master MISO ACK & Telemetry Packet Structure.
// A synchronous SPI transfer clocks the same number of bytes in both
// directions, so this must be the same size as ControllerSpiPacket.
//
// IMPORTANT: only the first 8 bytes are actually transmitted. The slave's
// PL022 TX FIFO holds exactly 8 entries and it preloads the whole ACK in one
// go while the master is idle between polls, so a 9th byte cannot be queued -
// it is silently discarded. That is fine for the reply, which has room to
// spare, so all meaningful fields and the CRC live in bytes 0..7. Byte 8 is
// whatever the slave shifts out once its FIFO drains; the master ignores it.
// (This is why the command packet may be 9 bytes but the ACK may not.)
struct ControllerSpiAckPacket {
    uint8_t  header;        // Byte 0: Always 0xA5 (ACK Header)
    uint8_t  slave_id;      // Byte 1: Target ID (0..3)
    uint8_t  status_flags;  // Byte 2: Bit 0 = Active, Bit 1 = Valid Packet Rx
    uint8_t  player_leds;   // Byte 3: Active Player LEDs (1..4)
    uint16_t packet_count;  // Bytes 4..5: Received Valid Packet Count
    uint8_t  reserved;      // Byte 6: 0x00
    uint8_t  crc8;          // Byte 7: Polynomial 0x07 over bytes 0..6
    uint8_t  pad;           // Byte 8: NOT transmitted - ignored, never trust it
};

// Bytes of the ACK that are transmitted and covered by its CRC.
#define SPI_ACK_VALID_BYTES 8
#pragma pack(pop)

static_assert(sizeof(ControllerSpiPacket) == sizeof(ControllerSpiAckPacket),
              "command and ACK packets must be the same size - a synchronous SPI "
              "transfer clocks equal byte counts in both directions");

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
