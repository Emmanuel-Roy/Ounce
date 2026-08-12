#include "spi_master.h"
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "hardware/spi.h"

const uint CS_PINS[4] = {20, 21, 22, 26};

void spi_master_init() {
    spi_init(SPI_PORT, 1 * 1000 * 1000);
    spi_set_format(SPI_PORT, 8, SPI_CPOL_0, SPI_CPHA_1, SPI_MSB_FIRST);

    gpio_set_function(PIN_MISO, GPIO_FUNC_SPI);
    gpio_set_function(PIN_SCK, GPIO_FUNC_SPI);
    gpio_set_function(PIN_MOSI, GPIO_FUNC_SPI);

    gpio_pull_up(PIN_MISO);

    // Initialize all candidate CS pins (GP17, GP20, GP21, GP22, GP26) HIGH
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

bool spi_master_transceive_packet(uint8_t slave_index, const ControllerSpiPacket& tx_packet, ControllerSpiAckPacket& rx_ack) {
    if (slave_index >= 4) return false;

    if (slave_index == 0) {
        gpio_put(17, 0);
        gpio_put(20, 0);
    } else {
        gpio_put(CS_PINS[slave_index], 0);
    }
    sleep_us(10);

    spi_write_read_blocking(SPI_PORT, reinterpret_cast<const uint8_t*>(&tx_packet), reinterpret_cast<uint8_t*>(&rx_ack), sizeof(ControllerSpiPacket));

    sleep_us(10);

    if (slave_index == 0) {
        gpio_put(17, 1);
        gpio_put(20, 1);
    } else {
        gpio_put(CS_PINS[slave_index], 1);
    }
    sleep_us(10);

    if (rx_ack.header == 0xA5 && rx_ack.slave_id == slave_index) {
        uint8_t expected_crc = calculate_crc8(reinterpret_cast<const uint8_t*>(&rx_ack), 7);
        if (rx_ack.crc8 == expected_crc) {
            return true;
        }
    }

    ControllerSpiAckPacket normalized_ack = rx_ack;
    normalize_ack_stream(normalized_ack);
    if (normalized_ack.header == 0xA5 && normalized_ack.slave_id == slave_index) {
        uint8_t expected_crc = calculate_crc8(reinterpret_cast<const uint8_t*>(&normalized_ack), 7);
        if (normalized_ack.crc8 == expected_crc) {
            rx_ack = normalized_ack;
            return true;
        }
    }

    if (rx_ack.header == 0x5A) {
        return true;
    }

    return false;
}
