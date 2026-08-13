#include "spi_master.h"
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "hardware/spi.h"

const uint CS_PINS[4] = {20, 21, 22, 26};

// unreset_block_wait() polls resets_hw->reset_done with no timeout at all.
// spi_master_init() runs this on every RECOVER event, not just at boot, so a
// stuck reset-done bit hangs the whole main loop exactly like the unbounded
// SPI FIFO polls did - see the comments below. Bounded so a wedge here can't
// take the firmware down with it either.
static void spi_master_reset_hw(uint32_t timeout_us) {
    reset_block(RESETS_RESET_SPI0_BITS);
    unreset_block(RESETS_RESET_SPI0_BITS);

    absolute_time_t deadline = make_timeout_time_us(timeout_us);
    while ((~resets_hw->reset_done) & RESETS_RESET_SPI0_BITS) {
        if (time_reached(deadline)) break;
    }
}

void spi_master_init() {
    // Hard reset the SPI peripheral (bounded - see spi_master_reset_hw)
    spi_master_reset_hw(2000);

    spi_init(SPI_PORT, 1 * 1000 * 1000);
    spi_set_format(SPI_PORT, 8, SPI_CPOL_0, SPI_CPHA_1, SPI_MSB_FIRST);  // Mode 1

    gpio_set_function(PIN_MISO, GPIO_FUNC_SPI);
    gpio_set_function(PIN_SCK,  GPIO_FUNC_SPI);
    gpio_set_function(PIN_MOSI, GPIO_FUNC_SPI);
    gpio_pull_up(PIN_MISO);

    // All CS pins high
    const uint all_cs[] = {17, 20, 21, 22, 26};
    for (size_t i = 0; i < 5; i++) {
        gpio_init(all_cs[i]);
        gpio_set_dir(all_cs[i], GPIO_OUT);
        gpio_put(all_cs[i], 1);
        gpio_pull_up(all_cs[i]);
    }
}

void spi_master_send_packet(uint8_t slave_index, const ControllerSpiPacket& packet) {
    ControllerSpiAckPacket ack_dummy;
    spi_master_transceive_packet(slave_index, packet, ack_dummy);
}

static void normalize_ack_stream(ControllerSpiAckPacket &ack) {
    uint8_t *buf = reinterpret_cast<uint8_t*>(&ack);
    for (size_t i = 0; i < sizeof(ControllerSpiAckPacket) - 1; i++) {
        buf[i] = (buf[i] << 1) | (buf[i + 1] >> 7);
    }
    buf[sizeof(ControllerSpiAckPacket) - 1] <<= 1;
}

// spi_write_read_blocking() (and a bare spi_is_readable() drain loop) poll the
// SPI0 FIFO flags with no deadline at all. If the peripheral ever wedges
// (stalled clock after a reset, electrical glitch, etc.) either one spins
// the CPU forever - no more SPI, no more prints, nothing - and the recovery
// logic below never gets a chance to run because execution never returns.
// These bounded versions fail fast instead so a wedge is just another
// failed transceive that the existing retry/reset logic can recover from.
static bool drain_stale_rx(uint32_t timeout_us) {
    absolute_time_t deadline = make_timeout_time_us(timeout_us);
    while (spi_is_readable(SPI_PORT)) {
        (void)spi_get_hw(SPI_PORT)->dr;
        if (time_reached(deadline)) return false;
    }
    return true;
}

static bool spi_transfer_with_timeout(const uint8_t *src, uint8_t *dst, size_t len, uint32_t timeout_us) {
    absolute_time_t deadline = make_timeout_time_us(timeout_us);
    size_t tx_remaining = len;
    size_t rx_remaining = len;
    const size_t fifo_depth = 8;

    while (tx_remaining || rx_remaining) {
        if (tx_remaining && spi_is_writable(SPI_PORT) && rx_remaining < tx_remaining + fifo_depth) {
            spi_get_hw(SPI_PORT)->dr = static_cast<uint32_t>(*src++);
            --tx_remaining;
        }
        if (rx_remaining && spi_is_readable(SPI_PORT)) {
            *dst++ = static_cast<uint8_t>(spi_get_hw(SPI_PORT)->dr);
            --rx_remaining;
        }
        if (time_reached(deadline)) return false;
    }
    return true;
}

bool spi_master_transceive_packet(uint8_t slave_index,
                                  const ControllerSpiPacket& tx_packet,
                                  ControllerSpiAckPacket& rx_ack)
{
    if (slave_index >= 4) return false;

    // Drain stale RX (bounded - see comment above drain_stale_rx)
    drain_stale_rx(200);

    // Assert CS (only GP20 for slave 0)
    if (slave_index == 0) {
        gpio_put(20, 0);
    } else {
        gpio_put(CS_PINS[slave_index], 0);
    }
    sleep_us(30);

    bool xfer_ok = spi_transfer_with_timeout(
                            reinterpret_cast<const uint8_t*>(&tx_packet),
                            reinterpret_cast<uint8_t*>(&rx_ack),
                            sizeof(ControllerSpiPacket),
                            2000);

    sleep_us(30);

    if (slave_index == 0) {
        gpio_put(20, 1);
    } else {
        gpio_put(CS_PINS[slave_index], 1);
    }
    sleep_us(50);

    if (!xfer_ok) return false;

    // CRC check
    if (rx_ack.header == 0xA5 && rx_ack.slave_id == slave_index) {
        uint8_t expected = calculate_crc8(reinterpret_cast<const uint8_t*>(&rx_ack), 7);
        if (rx_ack.crc8 == expected) return true;
    }

    // 1-bit normalize fallback
    ControllerSpiAckPacket normalized = rx_ack;
    normalize_ack_stream(normalized);
    if (normalized.header == 0xA5 && normalized.slave_id == slave_index) {
        uint8_t expected = calculate_crc8(reinterpret_cast<const uint8_t*>(&normalized), 7);
        if (normalized.crc8 == expected) {
            rx_ack = normalized;
            return true;
        }
    }

    return false;
}
