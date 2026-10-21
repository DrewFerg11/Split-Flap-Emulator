/* SPDX-License-Identifier: GPL-2.0-only */
/* ESP32 has a flat address space: program-memory accessors are plain memory accesses. */
#pragma once
#include <cstring>

#define PGM_P const char *
#define PGM_VOID_P const void *
#define PSTR(s) (s)
#define pgm_read_byte(address) (*(const unsigned char *)(address))
#define pgm_read_byte_near(address) pgm_read_byte(address)
#define pgm_read_word(address) (*(const unsigned short *)(address))
#define pgm_read_dword(address) (*(const unsigned long *)(address))
#define pgm_read_ptr(address) (*(const void *const *)(address))
#define memcpy_P memcpy
#define memcmp_P memcmp
#define strlen_P strlen
#define strnlen_P strnlen
#define strcpy_P strcpy
#define strncpy_P strncpy
#define strcmp_P strcmp
#define strncmp_P strncmp
#define strcat_P strcat
#define strstr_P strstr
#define sprintf_P sprintf
#define snprintf_P snprintf
#define vsnprintf_P vsnprintf
