#include <stddef.h>
#include <stdint.h>

#include "control_loop.h"

static int control_loop_session_reused(const struct control_loop *loop,
                                       uint32_t session_id);
static enum control_loop_result
control_loop_map_mailbox_session(enum control_mailbox_status status);
static enum control_loop_result
control_loop_map_watchdog_session(enum watchdog_status status);
static void control_loop_force_safe(struct control_loop *loop);
static void control_loop_latch_clock_fault(struct control_loop *loop,
                                           uint64_t now_ns,
                                           uint8_t poll_watchdog);
static uint8_t control_loop_is_safe(const struct control_loop *loop);
static int32_t control_loop_error_mC(int32_t target_mC,
                                     int32_t measured_mC);
static void control_loop_write_snapshot(
    const struct control_loop *loop,
    struct control_loop_status_snapshot *snapshot);
static enum control_loop_result control_loop_apply_command(
    struct control_loop *loop,
    const struct control_mailbox_command *command,
    uint64_t now_ns);

void control_loop_init(struct control_loop *loop, uint64_t now_ns)
{
    if (loop == NULL) {
        return;
    }

    *loop = (struct control_loop){0};
    control_mailbox_init(&loop->mailbox);
    watchdog_init(&loop->watchdog);
    plant_reset(&loop->plant);

    loop->last_release_ns = now_ns;
    loop->last_clock_ns = now_ns;
    loop->target_mC = PLANT_TARGET_TEMPERATURE_MC;
    loop->health_flags = CONTROL_LOOP_HEALTH_SAFE;
    loop->last_mailbox_status = CONTROL_MAILBOX_STATUS_EMPTY;
    loop->last_watchdog_status = WATCHDOG_STATUS_SAFE;
    loop->last_plant_status = PLANT_STATUS_OK;
    loop->initialized = UINT8_C(1);
    loop->has_clock = UINT8_C(1);
    loop->has_release = UINT8_C(1);
    loop->manual_safe = UINT8_C(1);
}

enum control_loop_result
control_loop_begin_session(struct control_loop *loop, uint32_t session_id,
                           uint64_t now_ns)
{
    struct control_mailbox mailbox_candidate;
    struct watchdog watchdog_candidate;
    enum control_mailbox_status mailbox_status;
    enum watchdog_status watchdog_status;

    if (loop == NULL) {
        return CONTROL_LOOP_RESULT_NULL_ARGUMENT;
    }
    if (!loop->initialized) {
        return CONTROL_LOOP_RESULT_NOT_INITIALIZED;
    }
    if (session_id == 0U) {
        return CONTROL_LOOP_RESULT_INVALID_SESSION;
    }
    if (loop->has_clock && now_ns < loop->last_clock_ns) {
        control_loop_latch_clock_fault(loop, now_ns, UINT8_C(1));
        return CONTROL_LOOP_RESULT_CLOCK_BACKWARD;
    }
    if (control_loop_session_reused(loop, session_id)) {
        return CONTROL_LOOP_RESULT_SESSION_NOT_NEW;
    }

    /* Validate both state machines on copies before committing either one. */
    mailbox_candidate = loop->mailbox;
    watchdog_candidate = loop->watchdog;
    mailbox_status = control_mailbox_begin_session(&mailbox_candidate,
                                                    session_id);
    watchdog_status = watchdog_begin_session(&watchdog_candidate, session_id,
                                              now_ns);
    loop->last_mailbox_status = mailbox_status;
    loop->last_watchdog_status = watchdog_status;

    if (mailbox_status != CONTROL_MAILBOX_STATUS_OK) {
        return control_loop_map_mailbox_session(mailbox_status);
    }
    if (watchdog_status != WATCHDOG_STATUS_ALIVE) {
        if (watchdog_status == WATCHDOG_STATUS_CLOCK_BACKWARD) {
            control_loop_latch_clock_fault(loop, now_ns, UINT8_C(1));
        }
        return control_loop_map_watchdog_session(watchdog_status);
    }

    loop->mailbox = mailbox_candidate;
    loop->watchdog = watchdog_candidate;
    loop->last_release_ns = now_ns;
    loop->last_clock_ns = now_ns;
    loop->has_clock = UINT8_C(1);
    loop->has_release = UINT8_C(1);
    loop->has_session = UINT8_C(1);
    loop->last_command = (struct control_mailbox_command){0};
    loop->target_mC = PLANT_TARGET_TEMPERATURE_MC;
    loop->health_flags = CONTROL_LOOP_HEALTH_SAFE;
    loop->manual_safe = UINT8_C(1);
    loop->clock_fault = UINT8_C(0);
    loop->last_plant_status = PLANT_STATUS_OK;
    /* Session changes are safe, but the plant sample index remains monotonic. */
    plant_enter_safe(&loop->plant);
    return CONTROL_LOOP_RESULT_OK;
}

enum control_mailbox_status
control_loop_publish(struct control_loop *loop,
                     const struct control_mailbox_command *command)
{
    enum control_mailbox_status status;

    if (loop == NULL || command == NULL) {
        return CONTROL_MAILBOX_STATUS_NULL_ARGUMENT;
    }
    if (!loop->initialized) {
        return CONTROL_MAILBOX_STATUS_INVALID_SESSION;
    }

    status = control_mailbox_publish(&loop->mailbox, command);
    loop->last_mailbox_status = status;
    return status;
}

enum control_loop_result
control_loop_release(struct control_loop *loop, uint64_t now_ns,
                     struct control_loop_status_snapshot *snapshot)
{
    struct control_mailbox_command command = {0};
    struct plant_step_result plant_result;
    enum control_mailbox_status mailbox_status;
    enum watchdog_status watchdog_status;
    enum control_loop_result result = CONTROL_LOOP_RESULT_OK;
    uint64_t elapsed_ns;

    if (loop == NULL || snapshot == NULL) {
        return CONTROL_LOOP_RESULT_NULL_ARGUMENT;
    }
    if (!loop->initialized) {
        return CONTROL_LOOP_RESULT_NOT_INITIALIZED;
    }

    /* A backward clock is a safety fault, even when this call is not due. */
    if (loop->has_clock && now_ns < loop->last_clock_ns) {
        control_loop_latch_clock_fault(loop, now_ns, UINT8_C(1));
        control_loop_write_snapshot(loop, snapshot);
        return CONTROL_LOOP_RESULT_CLOCK_BACKWARD;
    }
    loop->last_clock_ns = now_ns;
    loop->has_clock = UINT8_C(1);

    elapsed_ns = now_ns - loop->last_release_ns;
    if (elapsed_ns < CONTROL_LOOP_TICK_PERIOD_NS) {
        return CONTROL_LOOP_RESULT_NOT_DUE;
    }

    /* Execute one tick only; late calls do not run a catch-up loop. */
    loop->last_release_ns = now_ns;
    loop->has_release = UINT8_C(1);
    loop->health_flags &= (uint16_t)~CONTROL_LOOP_HEALTH_DEADLINE_MISS;
    if (elapsed_ns > CONTROL_LOOP_TICK_PERIOD_NS) {
        loop->health_flags |= CONTROL_LOOP_HEALTH_DEADLINE_MISS;
    }

    /* Exactly one non-blocking mailbox consume is performed for this tick. */
    mailbox_status = control_mailbox_take(&loop->mailbox, &command);
    loop->last_mailbox_status = mailbox_status;
    if (mailbox_status == CONTROL_MAILBOX_STATUS_OK) {
        result = control_loop_apply_command(loop, &command, now_ns);
    }

    /* This is the single watchdog poll for the due release. */
    watchdog_status = watchdog_poll(&loop->watchdog, now_ns);
    loop->last_watchdog_status = watchdog_status;
    if (watchdog_status == WATCHDOG_STATUS_CLOCK_BACKWARD) {
        control_loop_latch_clock_fault(loop, now_ns, UINT8_C(0));
        result = CONTROL_LOOP_RESULT_CLOCK_BACKWARD;
    } else if (watchdog_status == WATCHDOG_STATUS_EXPIRED) {
        loop->health_flags |= CONTROL_LOOP_HEALTH_NETWORK_TIMEOUT;
    }

    if (watchdog_safe_pending(&loop->watchdog) || loop->clock_fault ||
        loop->manual_safe || loop->plant.safe) {
        control_loop_force_safe(loop);
    } else {
        loop->health_flags &= (uint16_t)~CONTROL_LOOP_HEALTH_SAFE;
    }

    /* plant_step is called once for every due release, including SAFE ticks. */
    plant_result = plant_step(&loop->plant);
    loop->last_plant_status = plant_result.status;
    if (plant_result.status != PLANT_STATUS_OK) {
        loop->health_flags |= CONTROL_LOOP_HEALTH_SENSOR_INVALID;
        control_loop_force_safe(loop);
        if (result == CONTROL_LOOP_RESULT_OK) {
            result = CONTROL_LOOP_RESULT_PLANT_ERROR;
        }
    }

    if (control_loop_is_safe(loop)) {
        loop->health_flags |= CONTROL_LOOP_HEALTH_SAFE;
    } else {
        loop->health_flags &= (uint16_t)~CONTROL_LOOP_HEALTH_SAFE;
    }
    loop->health_flags &= CONTROL_LOOP_HEALTH_VALID_MASK;
    control_loop_write_snapshot(loop, snapshot);
    return result;
}

static int control_loop_session_reused(const struct control_loop *loop,
                                       uint32_t session_id)
{
    if (loop->mailbox.has_session &&
        loop->mailbox.active_session_id == session_id) {
        return 1;
    }
    if (loop->mailbox.has_previous_session &&
        loop->mailbox.previous_session_id == session_id) {
        return 1;
    }
    if (loop->watchdog.has_session &&
        loop->watchdog.active_session_id == session_id) {
        return 1;
    }
    if (loop->watchdog.has_previous_session &&
        loop->watchdog.previous_session_id == session_id) {
        return 1;
    }
    return 0;
}

static enum control_loop_result
control_loop_map_mailbox_session(enum control_mailbox_status status)
{
    if (status == CONTROL_MAILBOX_STATUS_INVALID_SESSION) {
        return CONTROL_LOOP_RESULT_INVALID_SESSION;
    }
    if (status == CONTROL_MAILBOX_STATUS_SESSION_NOT_NEW) {
        return CONTROL_LOOP_RESULT_SESSION_NOT_NEW;
    }
    return CONTROL_LOOP_RESULT_COMMAND_REJECTED;
}

static enum control_loop_result
control_loop_map_watchdog_session(enum watchdog_status status)
{
    if (status == WATCHDOG_STATUS_INVALID_SESSION) {
        return CONTROL_LOOP_RESULT_INVALID_SESSION;
    }
    if (status == WATCHDOG_STATUS_SESSION_NOT_NEW) {
        return CONTROL_LOOP_RESULT_SESSION_NOT_NEW;
    }
    if (status == WATCHDOG_STATUS_CLOCK_BACKWARD) {
        return CONTROL_LOOP_RESULT_CLOCK_BACKWARD;
    }
    return CONTROL_LOOP_RESULT_WATCHDOG_REJECTED;
}

static void control_loop_force_safe(struct control_loop *loop)
{
    plant_enter_safe(&loop->plant);
    loop->manual_safe = UINT8_C(1);
    loop->health_flags |= CONTROL_LOOP_HEALTH_SAFE;
}

static void control_loop_latch_clock_fault(struct control_loop *loop,
                                           uint64_t now_ns,
                                           uint8_t poll_watchdog)
{
    if (poll_watchdog) {
        loop->last_watchdog_status = watchdog_poll(&loop->watchdog, now_ns);
    }
    loop->clock_fault = UINT8_C(1);
    control_loop_force_safe(loop);
}

static uint8_t control_loop_is_safe(const struct control_loop *loop)
{
    if (loop->clock_fault || loop->manual_safe || loop->plant.safe ||
        watchdog_safe_pending(&loop->watchdog)) {
        return UINT8_C(1);
    }
    return UINT8_C(0);
}

static int32_t control_loop_error_mC(int32_t target_mC,
                                     int32_t measured_mC)
{
    const int64_t error_mC = (int64_t)target_mC - (int64_t)measured_mC;

    if (error_mC > INT64_C(2147483647)) {
        return INT32_MAX;
    }
    if (error_mC < -INT64_C(2147483648)) {
        return INT32_MIN;
    }
    return (int32_t)error_mC;
}

static void control_loop_write_snapshot(
    const struct control_loop *loop,
    struct control_loop_status_snapshot *snapshot)
{
    uint64_t sample_index = loop->plant.tick_index;

    if (sample_index > 0U) {
        sample_index -= UINT64_C(1);
    }
    snapshot->session_id = loop->has_session
                               ? loop->mailbox.active_session_id
                               : 0U;
    snapshot->sequence = loop->last_command.sequence;
    snapshot->request_id = loop->last_command.request_id;
    snapshot->sample_index = sample_index;
    snapshot->control_mode = loop->last_command.control_mode;
    snapshot->model_version = loop->last_command.model_version;
    snapshot->measured_mC = loop->plant.temperature_mC;
    snapshot->target_mC = loop->target_mC;
    snapshot->duty_q16_16 = loop->plant.duty_q16_16;
    snapshot->error_mC = control_loop_error_mC(snapshot->target_mC,
                                                snapshot->measured_mC);
    snapshot->health_flags = loop->health_flags &
                             CONTROL_LOOP_HEALTH_VALID_MASK;
    snapshot->safe = control_loop_is_safe(loop);
}

static enum control_loop_result control_loop_apply_command(
    struct control_loop *loop,
    const struct control_mailbox_command *command,
    uint64_t now_ns)
{
    enum watchdog_status watchdog_status;
    enum plant_status plant_status;

    if (!loop->has_session ||
        command->session_id != loop->mailbox.active_session_id ||
        loop->clock_fault) {
        loop->health_flags |= CONTROL_LOOP_HEALTH_ACTUATOR_CLAMPED;
        control_loop_force_safe(loop);
        return CONTROL_LOOP_RESULT_COMMAND_REJECTED;
    }

    switch (command->command) {
    case CONTROL_MAILBOX_APPLY_OUTPUT:
        /* A new valid output refreshes before the single poll below. */
        watchdog_status = watchdog_refresh_apply(
            &loop->watchdog, command->session_id, command->sequence,
            command->request_id, command->validity_ms, now_ns);
        loop->last_watchdog_status = watchdog_status;
        if (watchdog_status != WATCHDOG_STATUS_ALIVE) {
            if (watchdog_status == WATCHDOG_STATUS_CLOCK_BACKWARD) {
                control_loop_latch_clock_fault(loop, now_ns, UINT8_C(0));
                return CONTROL_LOOP_RESULT_CLOCK_BACKWARD;
            }
            control_loop_force_safe(loop);
            if (watchdog_status == WATCHDOG_STATUS_EXPIRED ||
                watchdog_status == WATCHDOG_STATUS_RECOVERY_REQUIRES_SESSION) {
                loop->health_flags |= CONTROL_LOOP_HEALTH_NETWORK_TIMEOUT;
            }
            return CONTROL_LOOP_RESULT_WATCHDOG_REJECTED;
        }

        plant_status = plant_apply_duty(&loop->plant,
                                         command->duty_q16_16);
        loop->last_plant_status = plant_status;
        if (plant_status != PLANT_STATUS_OK) {
            loop->health_flags |= CONTROL_LOOP_HEALTH_ACTUATOR_CLAMPED;
            control_loop_force_safe(loop);
            return CONTROL_LOOP_RESULT_PLANT_ERROR;
        }
        loop->last_command = *command;
        loop->manual_safe = UINT8_C(0);
        loop->health_flags &= (uint16_t)~CONTROL_LOOP_HEALTH_SAFE;
        return CONTROL_LOOP_RESULT_OK;

    case CONTROL_MAILBOX_ENTER_SAFE:
        plant_enter_safe(&loop->plant);
        loop->last_command = *command;
        loop->manual_safe = UINT8_C(1);
        loop->health_flags |= CONTROL_LOOP_HEALTH_SAFE;
        return CONTROL_LOOP_RESULT_OK;

    case CONTROL_MAILBOX_SET_TARGET:
        loop->target_mC = command->target_mC;
        loop->last_command = *command;
        return CONTROL_LOOP_RESULT_OK;

    default:
        /* A mailbox invariant was violated; keep the actuator fail-closed. */
        loop->health_flags |= CONTROL_LOOP_HEALTH_ACTUATOR_CLAMPED;
        control_loop_force_safe(loop);
        return CONTROL_LOOP_RESULT_COMMAND_REJECTED;
    }
}
