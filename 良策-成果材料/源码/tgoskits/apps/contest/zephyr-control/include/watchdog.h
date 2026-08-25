#ifndef CONTEST_ZEPHYR_CONTROL_WATCHDOG_H
#define CONTEST_ZEPHYR_CONTROL_WATCHDOG_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define WATCHDOG_VALIDITY_MS UINT16_C(500)
#define WATCHDOG_TIMEOUT_MS UINT64_C(500)
#define WATCHDOG_TIMEOUT_NS UINT64_C(500000000)

enum watchdog_status {
    WATCHDOG_STATUS_ALIVE = 0,
    WATCHDOG_STATUS_SAFE,
    WATCHDOG_STATUS_EXPIRED,
    WATCHDOG_STATUS_NULL_ARGUMENT,
    WATCHDOG_STATUS_INVALID_SESSION,
    WATCHDOG_STATUS_NO_SESSION,
    WATCHDOG_STATUS_SESSION_MISMATCH,
    WATCHDOG_STATUS_SESSION_NOT_NEW,
    WATCHDOG_STATUS_INVALID_SEQUENCE,
    WATCHDOG_STATUS_INVALID_REQUEST,
    WATCHDOG_STATUS_INVALID_VALIDITY,
    WATCHDOG_STATUS_DUPLICATE,
    WATCHDOG_STATUS_STALE_SEQUENCE,
    WATCHDOG_STATUS_STALE_REQUEST,
    WATCHDOG_STATUS_AMBIGUOUS_ORDER,
    WATCHDOG_STATUS_RECOVERY_REQUIRES_SESSION,
    WATCHDOG_STATUS_CLOCK_BACKWARD,
};

struct watchdog {
    uint32_t active_session_id;
    uint32_t previous_session_id;
    uint32_t last_sequence;
    uint32_t last_request_id;
    uint64_t session_start_ns;
    uint64_t last_apply_ns;
    uint64_t last_observed_ns;
    uint8_t has_session;
    uint8_t has_previous_session;
    uint8_t has_apply;
    uint8_t has_observed_time;
    uint8_t safe_pending;
    uint8_t requires_new_session;
};

/**
 * Initialize a watchdog in safe state with no active session or valid output.
 */
void watchdog_init(struct watchdog *watchdog);

/**
 * Begin a locally-created session at a receiver monotonic timestamp.
 *
 * A new session starts safe and has no valid output deadline. The old session
 * cannot refresh this watchdog after the switch.
 */
enum watchdog_status watchdog_begin_session(struct watchdog *watchdog,
                                            uint32_t session_id,
                                            uint64_t now_ns);

/**
 * Accept one new, valid APPLY_OUTPUT and refresh the local 500 ms deadline.
 *
 * The timestamp is receiver-local monotonic time. Invalid validity, duplicate
 * identity, stale identity, wrong session, and backwards time do not refresh.
 */
enum watchdog_status watchdog_refresh_apply(struct watchdog *watchdog,
                                            uint32_t session_id,
                                            uint32_t sequence,
                                            uint32_t request_id,
                                            uint16_t validity_ms,
                                            uint64_t now_ns);

/**
 * Poll the watchdog at a local monotonic timestamp.
 *
 * Exactly 500 ms is expired. Once pending-safe, the state remains safe until
 * a newer valid APPLY_OUTPUT is accepted.
 */
enum watchdog_status watchdog_poll(struct watchdog *watchdog,
                                   uint64_t now_ns);

/**
 * Query the fail-closed pending-safe state. Null is considered safe.
 */
uint8_t watchdog_safe_pending(const struct watchdog *watchdog);

#ifdef __cplusplus
}
#endif

#endif
