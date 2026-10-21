/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include <cstdint>

/* FreeRTOS on host threads. Ticks are 1 ms of virtual time (configTICK_RATE_HZ 1000). */
typedef uint32_t TickType_t;
typedef int BaseType_t;
typedef unsigned int UBaseType_t;
typedef struct flapsim_task *TaskHandle_t;
typedef void (*TaskFunction_t)(void *);

#define configTICK_RATE_HZ 1000
#define portTICK_PERIOD_MS 1
#define portMAX_DELAY ((TickType_t)0xffffffffUL)
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define pdTICKS_TO_MS(ticks) ((uint32_t)(ticks))
#define pdFALSE 0
#define pdTRUE 1
#define pdPASS pdTRUE
#define pdFAIL pdFALSE
#define tskNO_AFFINITY 0x7fffffff
