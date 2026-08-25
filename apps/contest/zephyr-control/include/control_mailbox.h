#ifndef CONTEST_ZEPHYR_CONTROL_MAILBOX_H
#define CONTEST_ZEPHYR_CONTROL_MAILBOX_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CONTROL_MAILBOX_SCHEMA_VERSION UINT8_C(1)
#define CONTROL_MAILBOX_VALIDITY_MS UINT16_C(500)
#define CONTROL_MAILBOX_TARGET_MC INT32_C(55000)
#define CONTROL_MAILBOX_MIN_DUTY_Q16_16 INT32_C(0)
#define CONTROL_MAILBOX_MAX_DUTY_Q16_16 INT32_C(65536)

enum control_mailbox_command_type {
    CONTROL_MAILBOX_APPLY_OUTPUT = 1,
    CONTROL_MAILBOX_ENTER_SAFE = 2,
    CONTROL_MAILBOX_SET_TARGET = 3,
};

enum control_mailbox_mode {
    CONTROL_MAILBOX_FIXED_BASELINE = 0,
    CONTROL_MAILBOX_MLP = 1,
};

enum control_mailbox_status {
    CONTROL_MAILBOX_STATUS_OK = 0,
    CONTROL_MAILBOX_STATUS_EMPTY,
    CONTROL_MAILBOX_STATUS_NULL_ARGUMENT,
    CONTROL_MAILBOX_STATUS_INVALID_SESSION,
    CONTROL_MAILBOX_STATUS_SESSION_MISMATCH,
    CONTROL_MAILBOX_STATUS_SESSION_NOT_NEW,
    CONTROL_MAILBOX_STATUS_INVALID_SEQUENCE,
    CONTROL_MAILBOX_STATUS_INVALID_REQUEST,
    CONTROL_MAILBOX_STATUS_INVALID_COMMAND,
    CONTROL_MAILBOX_STATUS_INVALID_MODE,
    CONTROL_MAILBOX_STATUS_INVALID_DUTY,
    CONTROL_MAILBOX_STATUS_INVALID_TARGET,
    CONTROL_MAILBOX_STATUS_INVALID_MODEL_VERSION,
    CONTROL_MAILBOX_STATUS_INVALID_VALIDITY,
    CONTROL_MAILBOX_STATUS_DUPLICATE,
    CONTROL_MAILBOX_STATUS_STALE_SEQUENCE,
    CONTROL_MAILBOX_STATUS_STALE_REQUEST,
    CONTROL_MAILBOX_STATUS_AMBIGUOUS_ORDER,
};

struct control_mailbox_command {
    uint32_t session_id;
    uint32_t sequence;
    uint32_t request_id;
    uint8_t command;
    uint8_t control_mode;
    int32_t duty_q16_16;
    int32_t target_mC;
    uint32_t model_version;
    uint16_t validity_ms;
};

struct control_mailbox {
    struct control_mailbox_command pending;
    uint32_t active_session_id;
    uint32_t previous_session_id;
    uint32_t last_sequence;
    uint32_t last_request_id;
    uint8_t has_pending;
    uint8_t has_session;
    uint8_t has_previous_session;
    uint8_t has_order;
};

/**
 * Initialize an empty, safe mailbox with no active session.
 */
void control_mailbox_init(struct control_mailbox *mailbox);

/**
 * Install a locally-created session and discard all pending commands from the
 * previous session. A session ID cannot be zero, repeated, or the immediately
 * previous session ID.
 */
enum control_mailbox_status
control_mailbox_begin_session(struct control_mailbox *mailbox,
                              uint32_t session_id);

/**
 * Validate and publish one command into the capacity-one latest-valid slot.
 *
 * The caller owns any transport parsing and must serialize calls to this
 * portable reference. A newer valid command replaces an unconsumed command;
 * stale and duplicate identities never replace it.
 */
enum control_mailbox_status
control_mailbox_publish(struct control_mailbox *mailbox,
                        const struct control_mailbox_command *command);

/**
 * Consume the newest pending command exactly once.
 */
enum control_mailbox_status
control_mailbox_take(struct control_mailbox *mailbox,
                     struct control_mailbox_command *command);

/**
 * Return non-zero when a command is waiting to be consumed. Null is treated
 * as empty for fail-closed polling.
 */
uint8_t control_mailbox_has_pending(const struct control_mailbox *mailbox);

#ifdef __cplusplus
}
#endif

#endif
