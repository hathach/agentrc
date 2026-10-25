cmake_minimum_required(VERSION 3.20)

# a TinyUSB checkout, its board's dependencies fetched (tools/get_deps.py -b <board>)
if (NOT TINYUSB_PATH)
  set(TINYUSB_PATH $ENV{TINYUSB_PATH})
endif ()
if (NOT TINYUSB_PATH)
  message(FATAL_ERROR "set TINYUSB_PATH, as -DTINYUSB_PATH or in the environment, to a TinyUSB checkout")
endif ()
include(${TINYUSB_PATH}/hw/bsp/family_support.cmake)

project(swo_fixture C ASM)

family_initialize_project(${PROJECT_NAME} ${CMAKE_CURRENT_LIST_DIR})

if (NOT BOARD MATCHES "^(stm32f407disco|stm32h743nucleo)$")
  message(FATAL_ERROR "the SWO fixture supports stm32f407disco and stm32h743nucleo only")
endif ()

add_executable(${PROJECT_NAME} src/swo_fixture.c)
target_include_directories(${PROJECT_NAME} PUBLIC src)
family_configure_device_example(${PROJECT_NAME} noos)
