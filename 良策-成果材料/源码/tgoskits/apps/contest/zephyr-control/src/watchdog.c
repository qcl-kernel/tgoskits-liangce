#include <stddef.h>
#include "watchdog.h"

enum watchdog_order {
    WATCHDOG_ORDER_OLDER = -1,
    WATCHDOG_ORDER_SAME = 0,
    WATCHDOG_ORDER_NEWER = 1,
    WATCHDOG_ORDER_AMBIGUOUS = 2,
};

static enum watchdog_status observe_time(struct watchdog *watchdog,
                                         uint64_t now_ns);
static enum watchdog_order compare_sequence(uint32_t candidate,
                                             uint32_t current);
static enum watchdog_status compare_identity(const struct watchdog *watchdog,
                                             uint32_t sequence,
                                             uint32_t request_id);

void watchdog_init(struct watchdog *watchdog)
{
    if (watchdog == NULL) {
        return;
    }

    *watchdog = (struct watchdog){0};
    watchdog->safe_pending = UINT8_C(1);
}

enum watchdog_status watchdog_begin_session(struct watchdog *watchdog,
                                            uint32_t session_id,
                                            uint64_t now_ns)
{
    enum watchdog_status time_status;

    if (watchdog == NULL) {
        return WATCHDOG_STATUS_NULL_ARGUMENT;
    }
    if (session_id == 0U) {
        return WATCHDOG_STATUS_INVALID_SESSION;
    }
    time_status = observe_time(watchdog, now_ns);
    if (time_status != WATCHDOG_STATUS_ALIVE) {
        return time_status;
    }
    if (watchdog->has_session && watchdog->active_session_id == session_id) {
        return WATCHDOG_STATUS_SESSION_NOT_NEW;
    }
    if (watchdog->has_previous_session &&
        watchdog->previous_session_id == session_id) {
        return WATCHDOG_STATUS_SESSION_NOT_NEW;
    }

    if (watchdog->has_session) {
        watchdog->previous_session_id = watchdog->active_session_id;
        watchdog->has_previous_session = UINT8_C(1);
    }
    watchdog->active_session_id = session_id;
    watchdog->session_start_ns = now_ns;
    watchdog->last_apply_ns = 0U;
    watchdog->last_sequence = 0U;
    watchdog->last_request_id = 0U;
    watchdog->has_session = UINT8_C(1);
    watchdog->has_apply = UINT8_C(0);
    watchdog->safe_pending = UINT8_C(1);
    watchdog->requires_new_session = UINT8_C(0);
    return WATCHDOG_STATUS_ALIVE;
}

enum watchdog_status watchdog_refresh_apply(struct watchdog *watchdog,
                                            uint32_t session_id,
                                            uint32_t sequence,
                                            uint32_t request_id,
                                            uint16_t validity_ms,
                                            uint64_t now_ns)
{
    enum watchdog_status time_status;
    enum watchdog_status identity_status;

    if (watchdog == NULL) {
        return WATCHDOG_STATUS_NULL_ARGUMENT;
    }
    time_status = observe_time(watchdog, now_ns);
    if (time_status == WATCHDOG_STATUS_CLOCK_BACKWARD) {
        return time_status;
    }
    if (session_id == 0U) {
        return WATCHDOG_STATUS_INVALID_SESSION;
    }
    if (!watchdog->has_session) {
        return WATCHDOG_STATUS_NO_SESSION;
    }
    if (session_id != watchdog->active_session_id) {
        return WATCHDOG_STATUS_SESSION_MISMATCH;
    }
    if (sequence == 0U) {
        return WATCHDOG_STATUS_INVALID_SEQUENCE;
    }
    if (request_id == 0U) {
        return WATCHDOG_STATUS_INVALID_REQUEST;
    }
    if (validity_ms != WATCHDOG_VALIDITY_MS) {
        return WATCHDOG_STATUS_INVALID_VALIDITY;
    }
    if (watchdog->requires_new_session) {
        return WATCHDOG_STATUS_RECOVERY_REQUIRES_SESSION;
    }
    if (watchdog->has_apply) {
        identity_status = compare_identity(watchdog, sequence, request_id);
        if (identity_status != WATCHDOG_STATUS_ALIVE) {
            return identity_status;
        }
    }

    watchdog->last_sequence = sequence;
    watchdog->last_request_id = request_id;
    watchdog->last_apply_ns = now_ns;
    watchdog->has_apply = UINT8_C(1);
    watchdog->safe_pending = UINT8_C(0);
    watchdog->requires_new_session = UINT8_C(0);
    return WATCHDOG_STATUS_ALIVE;
}

enum watchdog_status watchdog_poll(struct watchdog *watchdog,
                                   uint64_t now_ns)
{
    enum watchdog_status time_status;

    if (watchdog == NULL) {
        return WATCHDOG_STATUS_NULL_ARGUMENT;
    }
    time_status = observe_time(watchdog, now_ns);
    if (time_status == WATCHDOG_STATUS_CLOCK_BACKWARD) {
        watchdog->safe_pending = UINT8_C(1);
        return time_status;
    }
    if (!watchdog->has_session || !watchdog->has_apply) {
        watchdog->safe_pending = UINT8_C(1);
        return WATCHDOG_STATUS_SAFE;
    }
    if (watchdog->safe_pending) {
        return WATCHDOG_STATUS_EXPIRED;
    }
    if (now_ns - watchdog->last_apply_ns >= WATCHDOG_TIMEOUT_NS) {
        watchdog->safe_pending = UINT8_C(1);
        watchdog->requires_new_session = UINT8_C(1);
        return WATCHDOG_STATUS_EXPIRED;
    }
    return WATCHDOG_STATUS_ALIVE;
}

uint8_t watchdog_safe_pending(const struct watchdog *watchdog)
{
    if (watchdog == NULL) {
        return UINT8_C(1);
    }
    return watchdog->safe_pending;
}

static enum watchdog_status observe_time(struct watchdog *watchdog,
                                         uint64_t now_ns)
{
    if (watchdog->has_observed_time && now_ns < watchdog->last_observed_ns) {
        watchdog->safe_pending = UINT8_C(1);
        if (watchdog->has_apply) {
            watchdog->requires_new_session = UINT8_C(1);
        }
        return WATCHDOG_STATUS_CLOCK_BACKWARD;
    }

    watchdog->last_observed_ns = now_ns;
    watchdog->has_observed_time = UINT8_C(1);
    return WATCHDOG_STATUS_ALIVE;
}

static enum watchdog_order compare_sequence(uint32_t candidate,
                                             uint32_t current)
{
    const uint32_t difference = candidate - current;

    if (difference == 0U) {
        return WATCHDOG_ORDER_SAME;
    }
    if (difference == UINT32_C(0x80000000)) {
        return WATCHDOG_ORDER_AMBIGUOUS;
    }
    if (difference < UINT32_C(0x80000000)) {
        return WATCHDOG_ORDER_NEWER;
    }
    return WATCHDOG_ORDER_OLDER;
}

static enum watchdog_status compare_identity(const struct watchdog *watchdog,
                                             uint32_t sequence,
                                             uint32_t request_id)
{
    const enum watchdog_order sequence_order =
        compare_sequence(sequence, watchdog->last_sequence);
    const enum watchdog_order request_order =
        compare_sequence(request_id, watchdog->last_request_id);

    if (sequence_order == WATCHDOG_ORDER_AMBIGUOUS ||
        request_order == WATCHDOG_ORDER_AMBIGUOUS) {
        return WATCHDOG_STATUS_AMBIGUOUS_ORDER;
    }
    if (sequence_order == WATCHDOG_ORDER_SAME &&
        request_order == WATCHDOG_ORDER_SAME) {
        return WATCHDOG_STATUS_DUPLICATE;
    }
    if (sequence_order != WATCHDOG_ORDER_NEWER) {
        return WATCHDOG_STATUS_STALE_SEQUENCE;
    }
    if (request_order != WATCHDOG_ORDER_NEWER) {
        return WATCHDOG_STATUS_STALE_REQUEST;
    }
    return WATCHDOG_STATUS_ALIVE;
}
