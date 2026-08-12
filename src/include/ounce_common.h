#ifndef OUNCE_COMMON_H
#define OUNCE_COMMON_H

#include <cstdint>
#include <cstddef>

namespace Ounce {

// Sync Header Byte
constexpr uint8_t SPI_PACKET_HEADER = 0x5A;

// Bus & Timing Constants
constexpr uint32_t SPI_BAUD_RATE = 1000000; // 1 MHz for breadboard jumper wire signal integrity
constexpr uint32_t SPI_LOOP_INTERVAL_US = 1000; // 1 kHz (1000 us)
constexpr uint32_t SLAVE_WATCHDOG_TIMEOUT_MS = 50;

// Controller Slaves Configuration
constexpr uint8_t MAX_SLAVES = 8;
constexpr uint8_t ACTIVE_SLAVES = 4;

// Logical Button Masks matching GP2040-CE GamepadState
enum ButtonMask : uint16_t {
    BTN_B           = 1 << 0,  // Switch B
    BTN_A           = 1 << 1,  // Switch A
    BTN_Y           = 1 << 2,  // Switch Y
    BTN_X           = 1 << 3,  // Switch X
    BTN_L           = 1 << 4,  // Switch L
    BTN_R           = 1 << 5,  // Switch R
    BTN_ZL          = 1 << 6,  // Switch ZL
    BTN_ZR          = 1 << 7,  // Switch ZR
    BTN_MINUS       = 1 << 8,  // Switch Minus (-)
    BTN_PLUS        = 1 << 9,  // Switch Plus (+)
    BTN_L3          = 1 << 10, // Left Stick Click
    BTN_R3          = 1 << 11, // Right Stick Click
    BTN_HOME        = 1 << 12, // Home Button
    BTN_CAPTURE     = 1 << 13, // Capture Button
    DPAD_UP         = 1 << 14, // D-Pad Up
    DPAD_DOWN       = 1 << 15  // D-Pad Down
};

// 11-Byte Packed SPI Protocol Packet
#pragma pack(push, 1)
struct ControllerSpiPacket {
    uint8_t  header;    // Always 0x5A
    uint16_t buttons;   // Bitmask of ButtonMask
    uint8_t  lx;        // Left Stick X (0..255, center 128)
    uint8_t  ly;        // Left Stick Y (0..255, center 128)
    uint8_t  rx;        // Right Stick X (0..255, center 128)
    uint8_t  ry;        // Right Stick Y (0..255, center 128)
    uint8_t  lt;        // Left Trigger (0..255)
    uint8_t  rt;        // Right Trigger (0..255)
    uint8_t  sequence;  // Frame counter (0..255)
    uint8_t  crc8;      // CRC-8 checksum over bytes 0..9
};
#pragma pack(pop)

static_assert(sizeof(ControllerSpiPacket) == 11, "ControllerSpiPacket must be exactly 11 bytes!");

// Shared CRC-8 CCITT (Polynomial 0x07, Init 0x00)
inline uint8_t calculate_crc8(const uint8_t* data, size_t len) {
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (uint8_t j = 0; j < 8; ++j) {
            if (crc & 0x80) {
                crc = (crc << 1) ^ 0x07;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

// Reset packet to neutral state
inline void set_neutral_packet(ControllerSpiPacket& packet, uint8_t seq = 0) {
    packet.header = SPI_PACKET_HEADER;
    packet.buttons = 0;
    packet.lx = 128;
    packet.ly = 128;
    packet.rx = 128;
    packet.ry = 128;
    packet.lt = 0;
    packet.rt = 0;
    packet.sequence = seq;
    packet.crc8 = calculate_crc8(reinterpret_cast<const uint8_t*>(&packet), 10);
}

} // namespace Ounce

#endif // OUNCE_COMMON_H
