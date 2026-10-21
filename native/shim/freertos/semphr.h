/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "FreeRTOS.h"

/* Counting/binary semaphores and (non-recursive) mutexes with virtual-time timeouts. */
typedef struct flapsim_semaphore *SemaphoreHandle_t;

SemaphoreHandle_t xSemaphoreCreateCounting(UBaseType_t maximum, UBaseType_t initial);
SemaphoreHandle_t xSemaphoreCreateBinary(void);
SemaphoreHandle_t xSemaphoreCreateMutex(void);
BaseType_t xSemaphoreGive(SemaphoreHandle_t semaphore);
BaseType_t xSemaphoreTake(SemaphoreHandle_t semaphore, TickType_t ticks);
UBaseType_t uxSemaphoreGetCount(SemaphoreHandle_t semaphore);
void vSemaphoreDelete(SemaphoreHandle_t semaphore);
