#include "../src/linux_controller_state.h"

#include <stdio.h>

static int require(int condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "state contract failed: %s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    struct linux_controller_state state;
    struct linux_controller_state stale_state;
    struct linux_controller_control control;
    struct linux_controller_event event;
    unsigned event_count = 0U;

    linux_controller_init(&state, 1U, 17U);
    if (require(linux_controller_start(
                    &state, 25000, 55000, 1000U,
                    &control) == LINUX_CONTROLLER_CONTROL_READY,
                "initial plant sample must create one control") != 0 ||
        require(control.request_id == 1U && control.sequence == 1U &&
                    control.model_version == 731924617U,
                "MLP request identity is not canonical") != 0 ||
        require(linux_controller_mark_control_sent(&state, 1U, 2000U, 0U) ==
                    LINUX_CONTROLLER_CONTROL_READY,
                    "first send was not recorded") != 0 ||
        require(linux_controller_observe_ack(&state, 17U, 100U, 1U, 3000U) ==
                    LINUX_CONTROLLER_ACK_ACCEPTED,
                "matching ACK was not accepted") != 0 ||
        require(linux_controller_observe_status(
                    &state, 17U, 2U, 0U, 1U, 25100, 55000, 100000001U,
                    &control) == LINUX_CONTROLLER_CONTROL_READY,
                "feedback STATUS must permit the next control") != 0 ||
        require(control.request_id == 2U && control.sequence == 2U &&
                    control.sample_index == 1U,
                "request/sequence/sample did not advance") != 0 ||
        require(linux_controller_observe_status(
                    &state, 17U, 2U, 0U, 1U, 25100, 55000, 100000002U,
                    &control) == LINUX_CONTROLLER_DUPLICATE_STATUS,
                "duplicate STATUS created another inference") != 0 ||
        (linux_controller_init(&stale_state, 1U, 19U),
         require(linux_controller_observe_status(
                     &stale_state, 19U, 1U, 2U, 0U, 25000, 55000,
                     100000003U, &control) == LINUX_CONTROLLER_CONTROL_READY,
                 "stale-status fixture did not initialize") != 0 ||
         require(linux_controller_observe_status(
                     &stale_state, 19U, 2U, 1U, 0U, 25000, 55000,
                     100000004U, &control) == LINUX_CONTROLLER_STALE_STATUS,
                 "stale STATUS was not rejected")) != 0 ||
        require(linux_controller_observe_feedback(
                    &state, 17U, 3U, 2U, 1U, 25100, 100000005U) ==
                    LINUX_CONTROLLER_FEEDBACK_ACCEPTED,
                "feedback was not accepted") != 0 ||
        require(linux_controller_observe_ack(&state, 17U, 101U, 2U,
                                             100000006U) ==
                    LINUX_CONTROLLER_ACK_ACCEPTED,
                "second matching ACK was not accepted") != 0) {
        return 1;
    }

    while (linux_controller_pop_event(&state, &event) != 0) {
        if (event_count > 0U && event.monotonic_ns < 1000U) {
            return 1;
        }
        event_count += 1U;
    }
    if (require(event_count == 17U, "event sequence count drifted") != 0 ||
        require(state.event_dropped == 0U, "bounded event queue dropped a small run") != 0) {
        return 1;
    }
    puts("LINUX_CONTROLLER_STATE_HOST_PASS");
    return 0;
}
