#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>

#include <stdbool.h>
#include <stdint.h>

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

static void timer_expiry(struct k_timer *timer);
static void period_loop(void *unused_a, void *unused_b, void *unused_c);
static void logger_loop(void *unused_a, void *unused_b, void *unused_c);
static void append_event(enum probe_event_kind kind, uint32_t sample_index,
                         uint64_t raw_counter_ticks);
static bool take_event(struct probe_event *event);
static void emit_event(const struct probe_event *event);
static uint64_t counter_to_ns(uint64_t counter_ticks);
static const char *event_name(enum probe_event_kind kind);

K_SEM_DEFINE(period_sem, 0, K_SEM_MAX_LIMIT);
K_TIMER_DEFINE(period_timer, timer_expiry, NULL);
K_THREAD_STACK_DEFINE(period_stack, PROBE_THREAD_STACK_SIZE);
K_THREAD_STACK_DEFINE(logger_stack, PROBE_THREAD_STACK_SIZE);
static struct k_thread period_thread;
static struct k_thread logger_thread;

int main(void)
{
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
        append_event(PROBE_EVENT_PERIOD_START, sample_index, k_cycle_get_64());
        append_event(PROBE_EVENT_PERIOD_FINISH, sample_index, k_cycle_get_64());
        atomic_inc(&current_sample);
    }
}

static void logger_loop(void *unused_a, void *unused_b, void *unused_c)
{
    struct probe_event event;

    ARG_UNUSED(unused_a);
    ARG_UNUSED(unused_b);
    ARG_UNUSED(unused_c);

    while (true) {
        if (take_event(&event)) {
            emit_event(&event);
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

static void emit_event(const struct probe_event *event)
{
    uint64_t monotonic_ns = counter_to_ns(event->raw_counter_ticks);

    printk("{\"schema_version\":\"p3-rt-event-v1\","
           "\"run_id\":null,\"endpoint\":\"zephyr\","
           "\"scenario\":\"RT-NATIVE\",\"transport\":null,"
           "\"session_id\":null,\"sequence\":null,"
           "\"request_id\":null,\"sample_index\":%u,"
           "\"event\":\"%s\",\"monotonic_ns\":%llu,"
           "\"value\":null,\"unit\":\"ns\","
           "\"outcome\":\"observed\",\"clock_domain\":\"guest\","
           "\"raw_counter_ticks\":%llu,"
           "\"counter_frequency_hz\":%llu,"
           "\"clock_mapping_id\":\"native-guest-clock\","
           "\"vm_id\":null,\"vcpu_id\":null,\"pcpu_id\":null,"
           "\"interrupt_id\":null,\"path_variant\":\"native\","
           "\"path_id\":\"guest-period\",\"feature_state\":\"off\","
           "\"trace_sequence\":%u}\n",
           event->sample_index, event_name(event->kind),
           (unsigned long long)monotonic_ns,
           (unsigned long long)event->raw_counter_ticks,
           (unsigned long long)sys_clock_hw_cycles_per_sec(),
            event->trace_sequence);
}

static uint64_t counter_to_ns(uint64_t counter_ticks)
{
    uint64_t frequency_hz = sys_clock_hw_cycles_per_sec();
    uint64_t seconds;
    uint64_t remainder;

    if (frequency_hz == 0U) {
        return 0U;
    }
    seconds = counter_ticks / frequency_hz;
    remainder = counter_ticks % frequency_hz;
    return seconds * UINT64_C(1000000000) +
           remainder * UINT64_C(1000000000) / frequency_hz;
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
