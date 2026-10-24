#ifndef TUSB_CONFIG_H_
#define TUSB_CONFIG_H_

// CFG_TUSB_MCU comes from the BSP's compiler flags; the fixture uses the BSP only, no USB stack
#define CFG_TUSB_OS     OPT_OS_NONE
#define CFG_TUD_ENABLED 0
#define CFG_TUH_ENABLED 0

#endif
