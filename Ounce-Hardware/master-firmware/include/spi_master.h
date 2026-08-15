#ifndef SPI_MASTER_H
#define SPI_MASTER_H

#include "packet.h"
#include "pico/stdlib.h"
#include "hardware/spi.h"

#define SPI_PORT spi0
#define PIN_SCK  18  // SCK to Slaves (GP18) - shared, master drives
#define PIN_MOSI 19  // MOSI to Slaves (GP19) - shared, master drives

// Each slave gets its OWN MISO line. They cannot share one: the RP2040's SPI
// slave does not tri-state its TX pin when deselected (documented silicon
// limitation - raspberrypi/pico-feedback#227), so four slaves on one MISO net
// would be four push-pull drivers fighting each other.
//
// SPI0's RX can only be routed to these four pins, which is exactly how many
// we need. Only one may carry the SPI function at a time, so the master
// re-muxes before each transaction (see select_miso in spi_master.cpp).
extern const uint MISO_PINS[4];

// Dedicated Chip Select (CS) pins for Slaves 0..3. GP20 is a MISO now, so the
// selects moved up; GP23/24/25/29 are reserved by the Pico 2 W wireless chip.
extern const uint CS_PINS[4];

void spi_master_init();
void spi_master_send_packet(uint8_t slave_index, const ControllerSpiPacket& packet);
bool spi_master_transceive_packet(uint8_t slave_index, const ControllerSpiPacket& tx_packet, ControllerSpiAckPacket& rx_ack);

#endif // SPI_MASTER_H
