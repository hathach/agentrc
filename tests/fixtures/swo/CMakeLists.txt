cmake_minimum_required(VERSION 3.20)

# a TinyUSB checkout at the commit in tinyusb.pin, its board's dependencies fetched (tools/get_deps.py -b <board>)
if (NOT TINYUSB_PATH)
  message(FATAL_ERROR "set TINYUSB_PATH to a TinyUSB checkout")
endif ()
include(${TINYUSB_PATH}/hw/bsp/family_support.cmake)

# the fixture's rates rest on the BSP's clocks, measured at the pin
file(STRINGS ${CMAKE_CURRENT_LIST_DIR}/tinyusb.pin TINYUSB_PIN)
execute_process(COMMAND git -C ${TINYUSB_PATH} rev-parse HEAD
  OUTPUT_VARIABLE TINYUSB_HEAD OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if (NOT TINYUSB_HEAD STREQUAL TINYUSB_PIN)
  message(WARNING "TINYUSB_PATH is at '${TINYUSB_HEAD}', not the pin ${TINYUSB_PIN}: re-check the fixture's rates on hardware")
endif ()

project(swo_fixture C ASM)

family_initialize_project(${PROJECT_NAME} ${CMAKE_CURRENT_LIST_DIR})

if (NOT BOARD MATCHES "^(stm32f407disco|stm32h743nucleo)$")
  message(FATAL_ERROR "the SWO fixture supports stm32f407disco and stm32h743nucleo only")
endif ()

add_executable(${PROJECT_NAME} src/swo_fixture.c)
target_include_directories(${PROJECT_NAME} PUBLIC src)
family_configure_device_example(${PROJECT_NAME} noos)
