#include "linux_controller_state.h"

#include <string.h>

#include "../../common/icpc_payload.h"
#include "model/contest_model.h"

#define LINUX_CONTROLLER_TARGET_MC 55000
#define LINUX_CONTROLLER_MIN_MEASURED_MC (-40000)
#define LINUX_CONTROLLER_MAX_MEASURED_MC 125000
#define LINUX_CONTROLLER_MLP_MODE UINT8_C(1)
#define LINUX_CONTROLLER_FIXED_MODE UINT8_C(0)
#define LINUX_CONTROLLER_MLP_VERSION UINT32_C(731924617)

static void emit_event(struct linux_controller_state *state,
                       enum linux_controller_event_kind kind,
                       uint64_t monotonic_ns, uint32_t sequence,
                       uint32_t request_id, uint64_t sample_index,
                       int32_t value, uint8_t retransmission)
{
    uint8_t slot;
    struct linux_controller_event *event;

    (void)pthread_mutex_lock(&state->event_lock);
    if (state->event_count >= LINUX_CONTROLLER_EVENT_CAPACITY) {
        state->event_dropped += 1U;
        (void)pthread_mutex_unlock(&state->event_lock);
        return;
    }
    slot = (uint8_t)((state->event_head + state->event_count) %
                     LINUX_CONTROLLER_EVENT_CAPACITY);
    event = &state->events[slot];
    event->kind = kind;
    event->monotonic_ns = monotonic_ns;
    event->session_id = state->session_id;
    event->sequence = sequence;
    event->request_id = request_id;
    event->sample_index = sample_index;
    event->value = value;
    event->retransmission = retransmission;
    state->event_count += 1U;
    (void)pthread_mutex_unlock(&state->event_lock);
}

static void finish_period_if_ready(struct linux_controller_state *state,
                                   uint64_t monotonic_ns)
{
    if (state->pending.active != 0U &&
        state->pending.ack_received != 0U &&
        state->pending.feedback_received != 0U) {
        emit_event(state, LINUX_CONTROLLER_EVENT_PERIOD_FINISH,
                   monotonic_ns, 0U, state->pending.control.request_id,
                   state->pending.control.sample_index, 0, 0U);
        state->pending.active = UINT8_C(0);
    }
}

static uint32_t next_nonzero(uint32_t *value)
{
    uint32_t result = *value;
    *value += 1U;
    if (result == 0U) {
        result = *value;
        *value += 1U;
    }
    return result;
}

static enum linux_controller_result create_control(
    struct linux_controller_state *state, uint32_t input_sequence,
    uint64_t control_sample_index, int32_t measured_mC, int32_t target_mC,
    uint64_t monotonic_ns,
    struct linux_controller_control *control)
{
    int32_t duty;
    uint32_t request_id;
    uint32_t wire_sequence;

    if (target_mC != LINUX_CONTROLLER_TARGET_MC ||
        measured_mC < LINUX_CONTROLLER_MIN_MEASURED_MC ||
        measured_mC > LINUX_CONTROLLER_MAX_MEASURED_MC) {
        return LINUX_CONTROLLER_INVALID_STATUS;
    }
    if (state->controller_mode == LINUX_CONTROLLER_FIXED_MODE) {
        duty = contest_fixed_pi_update(&state->fixed_pi, measured_mC,
                                       target_mC);
    } else {
        duty = contest_control_duty(measured_mC, target_mC,
                                    state->previous_duty_q16_16);
    }
    if (duty < CONTEST_MIN_DUTY || duty > CONTEST_MAX_DUTY) {
        return LINUX_CONTROLLER_INVALID_STATUS;
    }
    request_id = next_nonzero(&state->next_request_id);
    wire_sequence = next_nonzero(&state->next_sequence);
    state->pending.control = (struct linux_controller_control){
        .session_id = state->session_id,
        .sequence = wire_sequence,
        .request_id = request_id,
        .sample_index = control_sample_index,
        .measured_mC = measured_mC,
        .target_mC = target_mC,
        .duty_q16_16 = duty,
        .model_version = state->controller_mode == LINUX_CONTROLLER_MLP_MODE
                             ? LINUX_CONTROLLER_MLP_VERSION
                             : 0U,
    };
    state->pending.active = UINT8_C(1);
    state->pending.ack_received = UINT8_C(0);
    state->pending.feedback_received = UINT8_C(0);
    state->previous_duty_q16_16 = duty;
    emit_event(state, LINUX_CONTROLLER_EVENT_PERIOD_RELEASE, monotonic_ns,
               0U, request_id, control_sample_index, 0, 0U);
    emit_event(state, LINUX_CONTROLLER_EVENT_PERIOD_START, monotonic_ns,
               0U, request_id, control_sample_index, 0, 0U);
    emit_event(state, LINUX_CONTROLLER_EVENT_INPUT_RECEIVE, monotonic_ns,
               input_sequence == 0U ? wire_sequence : input_sequence,
               request_id, control_sample_index, measured_mC, 0U);
    if (state->controller_mode == LINUX_CONTROLLER_MLP_MODE) {
        emit_event(state, LINUX_CONTROLLER_EVENT_INFERENCE_START,
                   monotonic_ns, 0U, request_id, control_sample_index,
                   0, 0U);
        emit_event(state, LINUX_CONTROLLER_EVENT_INFERENCE_FINISH,
                   monotonic_ns, 0U, request_id, control_sample_index, duty,
                   0U);
    }
    *control = state->pending.control;
    return LINUX_CONTROLLER_CONTROL_READY;
}

void linux_controller_init(struct linux_controller_state *state,
                           uint8_t controller_mode, uint32_t session_id)
{
    if (state == NULL) {
        return;
    }
    *state = (struct linux_controller_state){0};
    state->controller_mode = controller_mode;
    state->session_id = session_id;
    state->next_sequence = 1U;
    state->next_request_id = 1U;
    contest_fixed_pi_reset(&state->fixed_pi);
    (void)pthread_mutex_init(&state->event_lock, NULL);
}

enum linux_controller_result linux_controller_start(
    struct linux_controller_state *state, int32_t measured_mC,
    int32_t target_mC, uint64_t monotonic_ns,
    struct linux_controller_control *control)
{
    if (state == NULL || control == NULL) {
        return LINUX_CONTROLLER_NULL_ARGUMENT;
    }
    if (state->session_id == 0U || state->pending.active != 0U) {
        return LINUX_CONTROLLER_PENDING_FEEDBACK;
    }
    return create_control(state, 0U, 0U, measured_mC, target_mC, monotonic_ns,
                          control);
}

enum linux_controller_result linux_controller_observe_status(
    struct linux_controller_state *state, uint32_t session_id,
    uint32_t sequence, uint64_t sample_index, uint32_t applied_request_id,
    int32_t measured_mC, int32_t target_mC, uint64_t monotonic_ns,
    struct linux_controller_control *control)
{
    uint64_t next_sample_index;

    if (state == NULL || control == NULL) {
        return LINUX_CONTROLLER_NULL_ARGUMENT;
    }
    if (session_id == 0U || session_id != state->session_id || sequence == 0U ||
        target_mC != LINUX_CONTROLLER_TARGET_MC ||
        measured_mC < LINUX_CONTROLLER_MIN_MEASURED_MC ||
        measured_mC > LINUX_CONTROLLER_MAX_MEASURED_MC ||
        state->controller_mode > LINUX_CONTROLLER_MLP_MODE ||
        applied_request_id == UINT32_MAX) {
        return LINUX_CONTROLLER_INVALID_STATUS;
    }

    /* A STATUS carrying the applied request is the only feedback gate. */
    if (applied_request_id != 0U && state->pending.active != 0U &&
        applied_request_id == state->pending.control.request_id) {
        (void)linux_controller_observe_feedback(
            state, session_id, sequence, applied_request_id, sample_index,
            measured_mC, monotonic_ns);
    }
    if (state->has_sample != 0U) {
        if (sample_index == state->last_sample_index) {
            return LINUX_CONTROLLER_DUPLICATE_STATUS;
        }
        if (sample_index < state->last_sample_index) {
            return LINUX_CONTROLLER_STALE_STATUS;
        }
    }
    if (state->pending.active != 0U) {
        /* Do not overwrite a request whose feedback has not been confirmed. */
        return LINUX_CONTROLLER_PENDING_FEEDBACK;
    }

    if (sample_index == UINT64_MAX) {
        return LINUX_CONTROLLER_INVALID_STATUS;
    }
    next_sample_index = sample_index + 1U;
    state->last_sample_index = sample_index;
    state->has_sample = UINT8_C(1);
    return create_control(state, sequence, next_sample_index, measured_mC,
                          target_mC, monotonic_ns, control);
}

enum linux_controller_result linux_controller_mark_control_sent(
    struct linux_controller_state *state, uint32_t request_id,
    uint64_t monotonic_ns, uint8_t retransmission)
{
    if (state == NULL) {
        return LINUX_CONTROLLER_NULL_ARGUMENT;
    }
    if (state->pending.active == 0U ||
        state->pending.control.request_id != request_id) {
        return LINUX_CONTROLLER_INVALID_FEEDBACK;
    }
    if (state->pending.first_send_ns == 0U) {
        state->pending.first_send_ns = monotonic_ns;
    }
    state->pending.last_send_ns = monotonic_ns;
    emit_event(state, LINUX_CONTROLLER_EVENT_PACKET_SEND, monotonic_ns,
               state->pending.control.sequence, request_id,
               state->pending.control.sample_index,
               state->pending.control.duty_q16_16, retransmission);
    return LINUX_CONTROLLER_CONTROL_READY;
}

enum linux_controller_result linux_controller_observe_ack(
    struct linux_controller_state *state, uint32_t session_id,
    uint32_t ack_wire_sequence, uint32_t ack_sequence, uint64_t monotonic_ns)
{
    if (state == NULL) {
        return LINUX_CONTROLLER_NULL_ARGUMENT;
    }
    if (state->pending.active == 0U || session_id != state->session_id ||
        ack_wire_sequence == 0U ||
        ack_sequence != state->pending.control.sequence) {
        return LINUX_CONTROLLER_INVALID_ACK;
    }
    if (state->pending.ack_received != 0U) {
        return LINUX_CONTROLLER_DUPLICATE_STATUS;
    }
    state->pending.ack_received = UINT8_C(1);
    emit_event(state, LINUX_CONTROLLER_EVENT_ACK_RECEIVE, monotonic_ns,
               ack_wire_sequence, state->pending.control.request_id,
               state->pending.control.sample_index, (int32_t)ack_sequence, 0U);
    finish_period_if_ready(state, monotonic_ns);
    return LINUX_CONTROLLER_ACK_ACCEPTED;
}

enum linux_controller_result linux_controller_observe_feedback(
    struct linux_controller_state *state, uint32_t session_id,
    uint32_t status_sequence, uint32_t request_id, uint64_t sample_index,
    int32_t measured_mC, uint64_t monotonic_ns)
{
    if (state == NULL) {
        return LINUX_CONTROLLER_NULL_ARGUMENT;
    }
    if (state->pending.active == 0U || session_id != state->session_id ||
        status_sequence == 0U ||
        request_id == 0U || request_id != state->pending.control.request_id ||
        sample_index < state->pending.control.sample_index ||
        measured_mC < LINUX_CONTROLLER_MIN_MEASURED_MC ||
        measured_mC > LINUX_CONTROLLER_MAX_MEASURED_MC) {
        return LINUX_CONTROLLER_INVALID_FEEDBACK;
    }
    if (state->pending.feedback_received != 0U) {
        return LINUX_CONTROLLER_DUPLICATE_STATUS;
    }
    state->pending.feedback_received = UINT8_C(1);
    emit_event(state, LINUX_CONTROLLER_EVENT_FEEDBACK_RECEIVE, monotonic_ns,
               status_sequence, request_id, sample_index, measured_mC, 0U);
    finish_period_if_ready(state, monotonic_ns);
    return LINUX_CONTROLLER_FEEDBACK_ACCEPTED;
}

int linux_controller_pop_event(struct linux_controller_state *state,
                               struct linux_controller_event *event)
{
    if (state == NULL || event == NULL) {
        return 0;
    }
    (void)pthread_mutex_lock(&state->event_lock);
    if (state->event_count == 0U) {
        (void)pthread_mutex_unlock(&state->event_lock);
        return 0;
    }
    *event = state->events[state->event_head];
    state->event_head = (uint8_t)((state->event_head + 1U) %
                                  LINUX_CONTROLLER_EVENT_CAPACITY);
    state->event_count -= 1U;
    (void)pthread_mutex_unlock(&state->event_lock);
    return 1;
}
