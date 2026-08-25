#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>

#include <stdbool.h>
#include <stdint.h>

#include "p3_probe_config.h"

#define PROBE_PERIOD_MS 100
#define PROBE_RING_CAPACITY 1024
#define PROBE_THREAD_STACK_SIZE 2048

enum probe_event_kind {
    PROBE_EVENT_PERIOD_RELEASE,
    PROBE_EVENT_PERIOD_START,
    PROBE_EVENT_PERIOD_FINISH,
};

struct probe_event {
    enum probe_event_kind kind;
    uint32_t sample_index;
    uint64_t raw_counter_ticks;
    uint32_t trace_sequence;
};

static struct probe_event event_ring[PROBE_RING_CAPACITY];
static struct k_spinlock event_ring_lock;
static uint32_t event_ring_head;
static uint32_t event_ring_tail;
static uint32_t event_drop_count;
static atomic_t current_sample;
static atomic_t next_trace_sequence;
static atomic_t probe_complete;

static void timer_expiry(struct k_timer *timer);
static void period_loop(void *unused_a, void *unused_b, void *unused_c);
static void logger_loop(void *unused_a, void *unused_b, void *unused_c);
static void append_event(enum probe_event_kind kind, uint32_t sample_index,
                         uint64_t raw_counter_ticks);
static bool take_event(struct probe_event *event);
static uint32_t snapshot_drop_count(void);
static void emit_event(const struct probe_event *event);
static bool map_counter_ticks(uint64_t guest_ticks, uint64_t *host_ticks);
static bool counter_to_ns(uint64_t counter_ticks, uint64_t *monotonic_ns);
static const char *event_name(enum probe_event_kind kind);

K_SEM_DEFINE(period_sem, 0, K_SEM_MAX_LIMIT);
K_TIMER_DEFINE(period_timer, timer_expiry, NULL);
K_THREAD_STACK_DEFINE(period_stack, PROBE_THREAD_STACK_SIZE);
K_THREAD_STACK_DEFINE(logger_stack, PROBE_THREAD_STACK_SIZE);
static struct k_thread period_thread;
static struct k_thread logger_thread;

int main(void)
{
    printk("P3_RT_PROBE_READY run_id=%s scenario=%s mapping=%s "
           "frequency=%llu cntvoff=%lld vm=%s vcpu=%s pcpu=%s\n",
           P3_RUN_ID, P3_SCENARIO, P3_CLOCK_MAPPING_ID,
           (unsigned long long)sys_clock_hw_cycles_per_sec(),
           (long long)P3_CNTVOFF_TICKS, P3_VM_ID_JSON,
           P3_VCPU_ID_JSON, P3_PCPU_ID_JSON);
    k_thread_create(&period_thread, period_stack,
                    K_THREAD_STACK_SIZEOF(period_stack), period_loop, NULL,
                    NULL, NULL, 1, 0, K_NO_WAIT);
    k_thread_create(&logger_thread, logger_stack,
                    K_THREAD_STACK_SIZEOF(logger_stack), logger_loop, NULL,
                    NULL, NULL, 7, 0, K_NO_WAIT);
    k_timer_start(&period_timer, K_NO_WAIT, K_MSEC(PROBE_PERIOD_MS));
    return 0;
}

static void timer_expiry(struct k_timer *timer)
{
    ARG_UNUSED(timer);
    if ((uint32_t)atomic_get(&current_sample) >= P3_SAMPLE_COUNT) {
        return;
    }
    append_event(PROBE_EVENT_PERIOD_RELEASE,
                 (uint32_t)atomic_get(&current_sample), k_cycle_get_64());
    k_sem_give(&period_sem);
}

static void period_loop(void *unused_a, void *unused_b, void *unused_c)
{
    ARG_UNUSED(unused_a);
    ARG_UNUSED(unused_b);
    ARG_UNUSED(unused_c);

    while (true) {
        uint32_t sample_index;

        k_sem_take(&period_sem, K_FOREVER);
        sample_index = (uint32_t)atomic_get(&current_sample);
        if (sample_index >= P3_SAMPLE_COUNT) {
            continue;
        }
        append_event(PROBE_EVENT_PERIOD_START, sample_index, k_cycle_get_64());
        append_event(PROBE_EVENT_PERIOD_FINISH, sample_index, k_cycle_get_64());
        atomic_inc(&current_sample);
        if (sample_index + 1U == P3_SAMPLE_COUNT) {
            k_timer_stop(&period_timer);
            atomic_set(&probe_complete, 1);
        }
    }
}

static void logger_loop(void *unused_a, void *unused_b, void *unused_c)
{
    struct probe_event event;
    bool terminal_emitted = false;

    ARG_UNUSED(unused_a);
    ARG_UNUSED(unused_b);
    ARG_UNUSED(unused_c);

    while (true) {
        if (take_event(&event)) {
            emit_event(&event);
        } else if (!terminal_emitted && atomic_get(&probe_complete) != 0) {
            printk("P3_RT_PROBE_COMPLETE run_id=%s scenario=%s mapping=%s "
                   "samples=%u dropped=%u\n",
                   P3_RUN_ID, P3_SCENARIO, P3_CLOCK_MAPPING_ID,
                   P3_SAMPLE_COUNT, snapshot_drop_count());
            terminal_emitted = true;
        } else {
            k_sleep(K_MSEC(1));
        }
    }
}

static void append_event(enum probe_event_kind kind, uint32_t sample_index,
                         uint64_t raw_counter_ticks)
{
    k_spinlock_key_t key = k_spin_lock(&event_ring_lock);
    uint32_t next_head = (event_ring_head + 1U) % PROBE_RING_CAPACITY;

    if (next_head == event_ring_tail) {
        event_drop_count++;
        k_spin_unlock(&event_ring_lock, key);
        return;
    }
    event_ring[event_ring_head] = (struct probe_event){
        .kind = kind,
        .sample_index = sample_index,
        .raw_counter_ticks = raw_counter_ticks,
        .trace_sequence = (uint32_t)atomic_inc(&next_trace_sequence),
    };
    event_ring_head = next_head;
    k_spin_unlock(&event_ring_lock, key);
}

static bool take_event(struct probe_event *event)
{
    k_spinlock_key_t key;

    key = k_spin_lock(&event_ring_lock);
    if (event_ring_tail == event_ring_head) {
        k_spin_unlock(&event_ring_lock, key);
        return false;
    }
    *event = event_ring[event_ring_tail];
    event_ring_tail = (event_ring_tail + 1U) % PROBE_RING_CAPACITY;
    k_spin_unlock(&event_ring_lock, key);
    return true;
}

static uint32_t snapshot_drop_count(void)
{
    k_spinlock_key_t key = k_spin_lock(&event_ring_lock);
    uint32_t drops = event_drop_count;

    k_spin_unlock(&event_ring_lock, key);
    return drops;
}

static void emit_event(const struct probe_event *event)
{
    uint64_t monotonic_ns;

    if (!counter_to_ns(event->raw_counter_ticks, &monotonic_ns)) {
        printk("P3_RT_PROBE_FAIL reason=counter_mapping_overflow sample=%u\n",
               event->sample_index);
        return;
    }

    printk("{\"schema_version\":\"p3-rt-event-v1\","
           "\"run_id\":\"%s\",\"endpoint\":\"zephyr\","
           "\"scenario\":\"%s\",\"transport\":null,"
           "\"session_id\":null,\"sequence\":null,"
           "\"request_id\":null,\"sample_index\":%u,"
           "\"event\":\"%s\",\"monotonic_ns\":%llu,"
           "\"value\":null,\"unit\":\"ns\","
           "\"outcome\":\"observed\",\"clock_domain\":\"%s\","
           "\"raw_counter_ticks\":%llu,"
           "\"counter_frequency_hz\":%llu,"
           "\"clock_mapping_id\":\"%s\","
           "\"vm_id\":%s,\"vcpu_id\":%s,\"pcpu_id\":%s,"
           "\"interrupt_id\":null,\"path_variant\":\"%s\","
           "\"path_id\":\"%s\",\"feature_state\":\"%s\","
           "\"trace_sequence\":%u}\n",
           P3_RUN_ID, P3_SCENARIO, event->sample_index,
           event_name(event->kind),
           (unsigned long long)monotonic_ns,
           P3_CLOCK_DOMAIN,
           (unsigned long long)event->raw_counter_ticks,
           (unsigned long long)sys_clock_hw_cycles_per_sec(),
           P3_CLOCK_MAPPING_ID, P3_VM_ID_JSON, P3_VCPU_ID_JSON,
           P3_PCPU_ID_JSON, P3_PATH_VARIANT, P3_PATH_ID,
           P3_FEATURE_STATE, event->trace_sequence);
}

static bool map_counter_ticks(uint64_t guest_ticks, uint64_t *host_ticks)
{
    if (P3_CNTVOFF_TICKS >= 0) {
        uint64_t offset = (uint64_t)P3_CNTVOFF_TICKS;

        if (guest_ticks > UINT64_MAX - offset) {
            return false;
        }
        *host_ticks = guest_ticks + offset;
        return true;
    }

    uint64_t magnitude = (uint64_t)(-(P3_CNTVOFF_TICKS + 1)) + 1U;

    if (guest_ticks < magnitude) {
        return false;
    }
    *host_ticks = guest_ticks - magnitude;
    return true;
}

static bool counter_to_ns(uint64_t counter_ticks, uint64_t *monotonic_ns)
{
    uint64_t frequency_hz = sys_clock_hw_cycles_per_sec();
    uint64_t mapped_ticks;
    uint64_t seconds;
    uint64_t remainder;

    if (frequency_hz == 0U || !map_counter_ticks(counter_ticks, &mapped_ticks)) {
        return false;
    }
    seconds = mapped_ticks / frequency_hz;
    remainder = mapped_ticks % frequency_hz;
    if (seconds > UINT64_MAX / UINT64_C(1000000000)) {
        return false;
    }
    *monotonic_ns = seconds * UINT64_C(1000000000) +
                    remainder * UINT64_C(1000000000) / frequency_hz;
    return true;
}

static const char *event_name(enum probe_event_kind kind)
{
    switch (kind) {
    case PROBE_EVENT_PERIOD_RELEASE:
        return "period_release";
    case PROBE_EVENT_PERIOD_START:
        return "period_start";
    case PROBE_EVENT_PERIOD_FINISH:
        return "period_finish";
    default:
        return "unknown";
    }
}
