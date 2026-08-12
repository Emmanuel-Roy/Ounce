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
