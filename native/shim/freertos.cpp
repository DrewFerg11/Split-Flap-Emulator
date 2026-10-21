/* SPDX-License-Identifier: GPL-2.0-only */
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host.h"
#include "vclock.h"

#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>

struct flapsim_task {
    std::string name;
    BaseType_t core;
};

struct flapsim_semaphore {
    std::mutex mutex;
    std::condition_variable changed;
    UBaseType_t count, maximum;
};

namespace {

/* The Arduino loop task runs on core 1; other threads inherit core 0 unless pinned. */
thread_local flapsim_task *current = nullptr;
flapsim_task loop_task{"loopTask", 1};

struct TaskDeleted {};

} // namespace

BaseType_t xTaskCreatePinnedToCore(TaskFunction_t function, const char *name, uint32_t,
                                   void *parameter, UBaseType_t, TaskHandle_t *handle,
                                   BaseType_t core) {
    auto *task = new flapsim_task{name ? name : "", core};
    if (handle)
        *handle = task;
    std::thread([function, parameter, task] {
        current = task;
        try {
            function(parameter);
            /* Returning from a FreeRTOS task function is a fatal error on the device. */
            flapsim::host::panic("task returned without vTaskDelete");
        } catch (const TaskDeleted &) {
        }
        delete task;
    }).detach();
    return pdPASS;
}

BaseType_t xTaskCreate(TaskFunction_t function, const char *name, uint32_t stack_depth,
                       void *parameter, UBaseType_t priority, TaskHandle_t *handle) {
    return xTaskCreatePinnedToCore(function, name, stack_depth, parameter, priority, handle,
                                   tskNO_AFFINITY);
}

void vTaskDelete(TaskHandle_t task) {
    if (task != nullptr && task != current)
        flapsim::host::panic("vTaskDelete of another task is not supported natively");
    if (current == nullptr || current == &loop_task)
        flapsim::host::panic("the Arduino loop task cannot delete itself natively");
    throw TaskDeleted{};
}

void vTaskDelay(TickType_t ticks) { flapsim::vclock::sleep_ns(int64_t(ticks) * 1000000); }

TickType_t xTaskGetTickCount(void) { return TickType_t(flapsim::vclock::boot_ns() / 1000000); }

TaskHandle_t xTaskGetCurrentTaskHandle(void) { return current ? current : &loop_task; }

BaseType_t xPortGetCoreID(void) {
    BaseType_t core = xTaskGetCurrentTaskHandle()->core;
    return core == tskNO_AFFINITY ? 0 : core;
}

void taskYIELD(void) { std::this_thread::yield(); }

void flapsim_freertos_enter_loop_task() { current = &loop_task; }

SemaphoreHandle_t xSemaphoreCreateCounting(UBaseType_t maximum, UBaseType_t initial) {
    if (maximum == 0 || initial > maximum)
        return nullptr;
    auto *semaphore = new flapsim_semaphore;
    semaphore->count = initial;
    semaphore->maximum = maximum;
    return semaphore;
}

SemaphoreHandle_t xSemaphoreCreateBinary(void) { return xSemaphoreCreateCounting(1, 0); }

SemaphoreHandle_t xSemaphoreCreateMutex(void) { return xSemaphoreCreateCounting(1, 1); }

BaseType_t xSemaphoreGive(SemaphoreHandle_t semaphore) {
    std::lock_guard<std::mutex> lock(semaphore->mutex);
    if (semaphore->count >= semaphore->maximum)
        return pdFAIL;
    semaphore->count++;
    semaphore->changed.notify_one();
    return pdPASS;
}

BaseType_t xSemaphoreTake(SemaphoreHandle_t semaphore, TickType_t ticks) {
    std::unique_lock<std::mutex> lock(semaphore->mutex);
    auto available = [semaphore] { return semaphore->count > 0; };
    if (ticks == portMAX_DELAY)
        semaphore->changed.wait(lock, available);
    else if (!flapsim::vclock::wait_for(semaphore->changed, lock, int64_t(ticks) * 1000000,
                                        available))
        return pdFAIL;
    semaphore->count--;
    return pdPASS;
}

UBaseType_t uxSemaphoreGetCount(SemaphoreHandle_t semaphore) {
    std::lock_guard<std::mutex> lock(semaphore->mutex);
    return semaphore->count;
}

void vSemaphoreDelete(SemaphoreHandle_t semaphore) { delete semaphore; }
