/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "FreeRTOS.h"

/* Each task is a detached host thread. The core is recorded for diagnostics only. */
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t function, const char *name, uint32_t stack_depth,
                                   void *parameter, UBaseType_t priority, TaskHandle_t *handle,
                                   BaseType_t core);
BaseType_t xTaskCreate(TaskFunction_t function, const char *name, uint32_t stack_depth,
                       void *parameter, UBaseType_t priority, TaskHandle_t *handle);
/* Only self-deletion (NULL or the caller's own handle) is supported; it ends the thread. */
void vTaskDelete(TaskHandle_t task);
void vTaskDelay(TickType_t ticks);
TickType_t xTaskGetTickCount(void);
TaskHandle_t xTaskGetCurrentTaskHandle(void);
BaseType_t xPortGetCoreID(void);
void taskYIELD(void);
