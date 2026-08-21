#include <stddef.h>
#include "control_mailbox.h"

enum mailbox_order {
    MAILBOX_ORDER_OLDER = -1,
    MAILBOX_ORDER_SAME = 0,
    MAILBOX_ORDER_NEWER = 1,
    MAILBOX_ORDER_AMBIGUOUS = 2,
};

static enum control_mailbox_status validate_command(
    const struct control_mailbox_command *command);
static enum mailbox_order compare_sequence(uint32_t candidate,
                                            uint32_t current);
static enum control_mailbox_status compare_command_identity(
    const struct control_mailbox *mailbox,
    const struct control_mailbox_command *command);

void control_mailbox_init(struct control_mailbox *mailbox)
{
    if (mailbox == NULL) {
        return;
    }

    *mailbox = (struct control_mailbox){0};
}

enum control_mailbox_status
control_mailbox_begin_session(struct control_mailbox *mailbox,
                              uint32_t session_id)
{
    if (mailbox == NULL) {
        return CONTROL_MAILBOX_STATUS_NULL_ARGUMENT;
    }
    if (session_id == 0U) {
        return CONTROL_MAILBOX_STATUS_INVALID_SESSION;
    }
    if (mailbox->has_session && mailbox->active_session_id == session_id) {
        return CONTROL_MAILBOX_STATUS_SESSION_NOT_NEW;
    }
    if (mailbox->has_previous_session &&
        mailbox->previous_session_id == session_id) {
        return CONTROL_MAILBOX_STATUS_SESSION_NOT_NEW;
    }

    if (mailbox->has_session) {
        mailbox->previous_session_id = mailbox->active_session_id;
        mailbox->has_previous_session = UINT8_C(1);
    }
    mailbox->active_session_id = session_id;
    mailbox->has_session = UINT8_C(1);
    mailbox->has_order = UINT8_C(0);
    mailbox->has_pending = UINT8_C(0);
    mailbox->pending = (struct control_mailbox_command){0};
    mailbox->last_sequence = 0U;
    mailbox->last_request_id = 0U;
    return CONTROL_MAILBOX_STATUS_OK;
}

enum control_mailbox_status
control_mailbox_publish(struct control_mailbox *mailbox,
                        const struct control_mailbox_command *command)
{
    enum control_mailbox_status status;

    if (mailbox == NULL || command == NULL) {
        return CONTROL_MAILBOX_STATUS_NULL_ARGUMENT;
    }
    if (command->session_id == 0U) {
        return CONTROL_MAILBOX_STATUS_INVALID_SESSION;
    }
    if (!mailbox->has_session ||
        command->session_id != mailbox->active_session_id) {
        return CONTROL_MAILBOX_STATUS_SESSION_MISMATCH;
    }

    status = validate_command(command);
    if (status != CONTROL_MAILBOX_STATUS_OK) {
        return status;
    }
    if (mailbox->has_order) {
        status = compare_command_identity(mailbox, command);
        if (status != CONTROL_MAILBOX_STATUS_OK) {
            return status;
        }
    }

    mailbox->pending = *command;
    mailbox->has_pending = UINT8_C(1);
    mailbox->last_sequence = command->sequence;
    mailbox->last_request_id = command->request_id;
    mailbox->has_order = UINT8_C(1);
    return CONTROL_MAILBOX_STATUS_OK;
}

enum control_mailbox_status
control_mailbox_take(struct control_mailbox *mailbox,
                     struct control_mailbox_command *command)
{
    if (mailbox == NULL || command == NULL) {
        return CONTROL_MAILBOX_STATUS_NULL_ARGUMENT;
    }
    if (!mailbox->has_pending) {
        return CONTROL_MAILBOX_STATUS_EMPTY;
    }

    *command = mailbox->pending;
    mailbox->pending = (struct control_mailbox_command){0};
    mailbox->has_pending = UINT8_C(0);
    return CONTROL_MAILBOX_STATUS_OK;
}

uint8_t control_mailbox_has_pending(const struct control_mailbox *mailbox)
{
    if (mailbox == NULL) {
        return UINT8_C(0);
    }
    return mailbox->has_pending;
}

static enum control_mailbox_status validate_command(
    const struct control_mailbox_command *command)
{
    if (command->sequence == 0U) {
        return CONTROL_MAILBOX_STATUS_INVALID_SEQUENCE;
    }
    if (command->request_id == 0U) {
        return CONTROL_MAILBOX_STATUS_INVALID_REQUEST;
    }
    if (command->command < CONTROL_MAILBOX_APPLY_OUTPUT ||
        command->command > CONTROL_MAILBOX_SET_TARGET) {
        return CONTROL_MAILBOX_STATUS_INVALID_COMMAND;
    }
    if (command->control_mode > CONTROL_MAILBOX_MLP) {
        return CONTROL_MAILBOX_STATUS_INVALID_MODE;
    }
    if (command->target_mC != CONTROL_MAILBOX_TARGET_MC) {
        return CONTROL_MAILBOX_STATUS_INVALID_TARGET;
    }
    if (command->validity_ms != CONTROL_MAILBOX_VALIDITY_MS) {
        return CONTROL_MAILBOX_STATUS_INVALID_VALIDITY;
    }

    if (command->command == CONTROL_MAILBOX_APPLY_OUTPUT) {
        if (command->duty_q16_16 < CONTROL_MAILBOX_MIN_DUTY_Q16_16 ||
            command->duty_q16_16 > CONTROL_MAILBOX_MAX_DUTY_Q16_16) {
            return CONTROL_MAILBOX_STATUS_INVALID_DUTY;
        }
        if (command->control_mode == CONTROL_MAILBOX_MLP) {
            if (command->model_version == 0U) {
                return CONTROL_MAILBOX_STATUS_INVALID_MODEL_VERSION;
            }
        } else if (command->model_version != 0U) {
            return CONTROL_MAILBOX_STATUS_INVALID_MODEL_VERSION;
        }
        return CONTROL_MAILBOX_STATUS_OK;
    }

    if (command->duty_q16_16 != 0 || command->model_version != 0U) {
        return CONTROL_MAILBOX_STATUS_INVALID_DUTY;
    }
    return CONTROL_MAILBOX_STATUS_OK;
}

static enum mailbox_order compare_sequence(uint32_t candidate,
                                            uint32_t current)
{
    const uint32_t difference = candidate - current;

    if (difference == 0U) {
        return MAILBOX_ORDER_SAME;
    }
    if (difference == UINT32_C(0x80000000)) {
        return MAILBOX_ORDER_AMBIGUOUS;
    }
    if (difference < UINT32_C(0x80000000)) {
        return MAILBOX_ORDER_NEWER;
    }
    return MAILBOX_ORDER_OLDER;
}

static enum control_mailbox_status compare_command_identity(
    const struct control_mailbox *mailbox,
    const struct control_mailbox_command *command)
{
    const enum mailbox_order sequence_order =
        compare_sequence(command->sequence, mailbox->last_sequence);
    const enum mailbox_order request_order =
        compare_sequence(command->request_id, mailbox->last_request_id);

    if (sequence_order == MAILBOX_ORDER_AMBIGUOUS ||
        request_order == MAILBOX_ORDER_AMBIGUOUS) {
        return CONTROL_MAILBOX_STATUS_AMBIGUOUS_ORDER;
    }
    if (sequence_order == MAILBOX_ORDER_SAME &&
        request_order == MAILBOX_ORDER_SAME) {
        return CONTROL_MAILBOX_STATUS_DUPLICATE;
    }
    if (sequence_order != MAILBOX_ORDER_NEWER) {
        return CONTROL_MAILBOX_STATUS_STALE_SEQUENCE;
    }
    if (request_order != MAILBOX_ORDER_NEWER) {
        return CONTROL_MAILBOX_STATUS_STALE_REQUEST;
    }
    return CONTROL_MAILBOX_STATUS_OK;
}
