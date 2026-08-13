cmake_minimum_required(VERSION 3.13)

set(PICO_BOARD pico2_w CACHE STRING "Board type")

include(pico_sdk_import.cmake OPTIONAL)
if (NOT pico_sdk_import_included)
    if (DEFINED ENV{PICO_SDK_PATH})
        include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)
    endif()
endif()

project(ounce_master C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# Never let USB-CDC stdout block the control loop waiting for a host to read.
# Default is 500ms, which was stalling the SPI polling loop (and the
# console output) whenever the desktop-side reader fell behind.
add_compile_definitions(PICO_STDIO_USB_STDOUT_TIMEOUT_US=0)

# Disable pico_stdio_usb's IRQ-driven background tud_task(). With it on,
# tud_task() can fire from a hardware IRQ at any instruction, including
# mid-call inside our own direct tud_cdc_read()/tud_cdc_write_flush() calls
# in main.cpp. Both paths claim TinyUSB's internal _usbd_mutex (a blocking
# mutex with an effectively unbounded wait), and the SDK's own mutex.h
# explicitly warns against blocking mutex calls from an IRQ handler: if the
# IRQ lands mid-claim on mainline, it deadlocks against code that can only
# resume once the IRQ returns - which it never does. That's the root cause
# of the intermittent full hangs. Disabling this makes main.cpp's explicit
# tud_task() call (once per loop) the only thing that ever touches TinyUSB,
# so there is no concurrent context left to race against.
add_compile_definitions(PICO_STDIO_USB_ENABLE_IRQ_BACKGROUND_TASK=0)

pico_sdk_init()

add_executable(ounce_master
    src/main.cpp
    src/spi_master.cpp
)

target_include_directories(ounce_master PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/include
    ${CMAKE_CURRENT_LIST_DIR}/../../src/include
)

target_link_libraries(ounce_master PRIVATE
    pico_stdlib
    hardware_spi
    hardware_gpio
)

pico_enable_stdio_usb(ounce_master 1)
pico_enable_stdio_uart(ounce_master 0)

pico_add_extra_outputs(ounce_master)
