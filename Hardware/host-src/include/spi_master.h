#ifndef SPI_MASTER_H
#define SPI_MASTER_H

#include "packet.h"
#include "pico/stdlib.h"
#include "hardware/spi.h"

#define SPI_PORT spi0
#define PIN_MISO 16  // MISO input from Slaves (GP16)
#define PIN_SCK  18  // SCK to Slaves (GP18)
#define PIN_MOSI 19  // MOSI to Slaves (GP19)

// Dedicated Chip Select (CS) pins for Slaves 0 through 3 (GP20, GP21, GP22, GP26)
extern const uint CS_PINS[4];

void spi_master_init();
void spi_master_send_packet(uint8_t slave_index, const ControllerSpiPacket& packet);
bool spi_master_transceive_packet(uint8_t slave_index, const ControllerSpiPacket& tx_packet, ControllerSpiAckPacket& rx_ack);

#endif // SPI_MASTER_H
