#ifndef CONTEST_LINUX_CONTROLLER_STATE_H
#define CONTEST_LINUX_CONTROLLER_STATE_H

#include <stddef.h>
#include <stdint.h>
#include <pthread.h>

#include "model/controller.h"

#define LINUX_CONTROLLER_EVENT_CAPACITY 64U

enum linux_controller_event_kind {
    LINUX_CONTROLLER_EVENT_PERIOD_RELEASE = 0,
    LINUX_CONTROLLER_EVENT_PERIOD_START,
    LINUX_CONTROLLER_EVENT_INPUT_RECEIVE,
    LINUX_CONTROLLER_EVENT_INFERENCE_START,
    LINUX_CONTROLLER_EVENT_INFERENCE_FINISH,
    LINUX_CONTROLLER_EVENT_PACKET_SEND,
    LINUX_CONTROLLER_EVENT_ACK_RECEIVE,
    LINUX_CONTROLLER_EVENT_FEEDBACK_RECEIVE,
    LINUX_CONTROLLER_EVENT_PERIOD_FINISH,
};

enum linux_controller_result {
    LINUX_CONTROLLER_CONTROL_READY = 0,
    LINUX_CONTROLLER_DUPLICATE_STATUS,
    LINUX_CONTROLLER_STALE_STATUS,
    LINUX_CONTROLLER_INVALID_STATUS,
    LINUX_CONTROLLER_PENDING_FEEDBACK,
    LINUX_CONTROLLER_ACK_ACCEPTED,
    LINUX_CONTROLLER_FEEDBACK_ACCEPTED,
    LINUX_CONTROLLER_INVALID_ACK,
    LINUX_CONTROLLER_INVALID_FEEDBACK,
    LINUX_CONTROLLER_NULL_ARGUMENT,
};

struct linux_controller_event {
    enum linux_controller_event_kind kind;
    uint64_t monotonic_ns;
    uint32_t session_id;
    uint32_t sequence;
    uint32_t request_id;
    uint64_t sample_index;
    int32_t value;
    uint8_t retransmission;
};

struct linux_controller_control {
    uint32_t session_id;
    uint32_t sequence;
    uint32_t request_id;
    uint64_t sample_index;
    int32_t measured_mC;
    int32_t target_mC;
    int32_t duty_q16_16;
    uint32_t model_version;
};

struct linux_controller_pending {
    struct linux_controller_control control;
    uint64_t first_send_ns;
    uint64_t last_send_ns;
    uint8_t active;
    uint8_t ack_received;
    uint8_t feedback_received;
};

struct linux_controller_state {
    uint8_t controller_mode;
    uint32_t session_id;
    uint32_t next_sequence;
    uint32_t next_request_id;
    uint64_t last_sample_index;
    uint8_t has_sample;
    int32_t previous_duty_q16_16;
    struct contest_fixed_pi_state fixed_pi;
    struct linux_controller_pending pending;
    struct linux_controller_event events[LINUX_CONTROLLER_EVENT_CAPACITY];
    uint8_t event_head;
    uint8_t event_count;
    uint32_t event_dropped;
    pthread_mutex_t event_lock;
};

void linux_controller_init(struct linux_controller_state *state,
                           uint8_t controller_mode, uint32_t session_id);

/* Seed request 1 from the canonical initial plant sample. This is not a
 * received STATUS and therefore does not advance last_sample_index. */
enum linux_controller_result linux_controller_start(
    struct linux_controller_state *state, int32_t measured_mC,
    int32_t target_mC, uint64_t monotonic_ns,
    struct linux_controller_control *control);

enum linux_controller_result linux_controller_observe_status(
    struct linux_controller_state *state, uint32_t session_id,
    uint32_t sequence, uint64_t sample_index, uint32_t applied_request_id,
    int32_t measured_mC, int32_t target_mC, uint64_t monotonic_ns,
    struct linux_controller_control *control);

enum linux_controller_result linux_controller_mark_control_sent(
    struct linux_controller_state *state, uint32_t request_id,
    uint64_t monotonic_ns, uint8_t retransmission);

enum linux_controller_result linux_controller_observe_ack(
    struct linux_controller_state *state, uint32_t session_id,
    uint32_t ack_wire_sequence, uint32_t ack_sequence,
    uint64_t monotonic_ns);

enum linux_controller_result linux_controller_observe_feedback(
    struct linux_controller_state *state, uint32_t session_id,
    uint32_t status_sequence, uint32_t request_id, uint64_t sample_index,
    int32_t measured_mC, uint64_t monotonic_ns);

int linux_controller_pop_event(struct linux_controller_state *state,
                               struct linux_controller_event *event);

#endif /* CONTEST_LINUX_CONTROLLER_STATE_H */
