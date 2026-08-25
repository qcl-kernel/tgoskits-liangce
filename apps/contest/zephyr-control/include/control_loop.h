#ifndef CONTEST_ZEPHYR_CONTROL_LOOP_H
#define CONTEST_ZEPHYR_CONTROL_LOOP_H

#include <stdint.h>

#include "control_mailbox.h"
#include "plant.h"
#include "watchdog.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The period is a contract value, not a scheduler sleep interval. */
#define CONTROL_LOOP_TICK_PERIOD_NS UINT64_C(100000000)
#define CONTROL_LOOP_PERIOD_NS CONTROL_LOOP_TICK_PERIOD_NS
#define CONTROL_LOOP_SAFE_DUTY_Q16_16 PLANT_MIN_DUTY_Q16_16

/* These are the only health bits allowed in the frozen STATUS payload. */
enum control_loop_health {
    CONTROL_LOOP_HEALTH_NONE = UINT16_C(0),
    CONTROL_LOOP_HEALTH_SAFE = UINT16_C(1) << 0,
    CONTROL_LOOP_HEALTH_NETWORK_TIMEOUT = UINT16_C(1) << 1,
    CONTROL_LOOP_HEALTH_SENSOR_INVALID = UINT16_C(1) << 2,
    CONTROL_LOOP_HEALTH_ACTUATOR_CLAMPED = UINT16_C(1) << 3,
    CONTROL_LOOP_HEALTH_DEADLINE_MISS = UINT16_C(1) << 4,
};

#define CONTROL_LOOP_HEALTH_VALID_MASK UINT16_C(0x001f)

/* Results for session management and the periodic release API. */
enum control_loop_result {
    CONTROL_LOOP_RESULT_OK = 0,
    CONTROL_LOOP_RESULT_NOT_DUE,
    CONTROL_LOOP_RESULT_NULL_ARGUMENT,
    CONTROL_LOOP_RESULT_NOT_INITIALIZED,
    CONTROL_LOOP_RESULT_CLOCK_BACKWARD,
    CONTROL_LOOP_RESULT_INVALID_SESSION,
    CONTROL_LOOP_RESULT_SESSION_NOT_NEW,
    CONTROL_LOOP_RESULT_COMMAND_REJECTED,
    CONTROL_LOOP_RESULT_WATCHDOG_REJECTED,
    CONTROL_LOOP_RESULT_PLANT_ERROR,
};

/* Short aliases make the requested init/begin/publish/release API concise. */
#define CONTROL_LOOP_OK CONTROL_LOOP_RESULT_OK
#define CONTROL_LOOP_NOT_DUE CONTROL_LOOP_RESULT_NOT_DUE
#define CONTROL_LOOP_NULL_ARGUMENT CONTROL_LOOP_RESULT_NULL_ARGUMENT
#define CONTROL_LOOP_CLOCK_BACKWARD CONTROL_LOOP_RESULT_CLOCK_BACKWARD
#define CONTROL_LOOP_RELEASED CONTROL_LOOP_RESULT_OK
#define CONTROL_LOOP_RELEASE_NOT_DUE CONTROL_LOOP_RESULT_NOT_DUE

/*
 * One status snapshot produced by a due release. sequence/request_id identify
 * the last command applied at a period boundary and are zero initially.
 * control_mode/model_version are retained for the STATUS adapter; the core
 * loop does not parse or otherwise interpret model data.
 */
struct control_loop_status_snapshot {
    uint32_t session_id;
    uint32_t sequence;
    uint32_t request_id;
    uint64_t sample_index;
    uint8_t control_mode;
    uint32_t model_version;
    int32_t measured_mC;
    int32_t target_mC;
    int32_t duty_q16_16;
    int32_t error_mC;
    uint16_t health_flags;
    uint8_t safe;
};

typedef struct control_loop_status_snapshot control_loop_status_snapshot;

/*
 * All state is caller-owned. The mailbox is allocation-free; callers must
 * serialize publish with release when it is shared by network and period
 * threads.
 */
struct control_loop {
    struct control_mailbox mailbox;
    struct watchdog watchdog;
    struct plant_state plant;
    struct control_mailbox_command last_command;

    uint64_t last_release_ns;
    uint64_t last_clock_ns;
    int32_t target_mC;
    uint16_t health_flags;

    enum control_mailbox_status last_mailbox_status;
    enum watchdog_status last_watchdog_status;
    enum plant_status last_plant_status;

    uint8_t initialized;
    uint8_t has_clock;
    uint8_t has_release;
    uint8_t has_session;
    uint8_t manual_safe;
    uint8_t clock_fault;
};

/* Initialize all owned components and start the first interval SAFE. */
void control_loop_init(struct control_loop *loop, uint64_t now_ns);

/* Switch to a non-zero, non-retired session. A successful switch is SAFE. */
enum control_loop_result
control_loop_begin_session(struct control_loop *loop, uint32_t session_id,
                           uint64_t now_ns);

/*
 * Publish is only a mailbox operation. It never runs the plant or touches
 * the watchdog. Mailbox duplicate/stale/invalid errors are returned intact.
 */
enum control_mailbox_status
control_loop_publish(struct control_loop *loop,
                     const struct control_mailbox_command *command);

/*
 * Release at most one 100 ms tick. NOT_DUE does no control work and leaves
 * snapshot untouched. A due release takes one mailbox snapshot, refreshes a
 * newly accepted APPLY_OUTPUT if present, polls the watchdog once, makes the
 * safe decision, steps the plant once, and writes one snapshot. No socket,
 * logging, heap, or catch-up loop is permitted here.
 */
enum control_loop_result
control_loop_release(struct control_loop *loop, uint64_t now_ns,
                     struct control_loop_status_snapshot *snapshot);

#ifdef __cplusplus
}
#endif

#endif
