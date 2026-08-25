#include "control_mailbox.h"
#include "plant.h"

#include <stdio.h>

static int expect_status(int actual, int expected, const char *description)
{
    if (actual == expected) {
        return 0;
    }
    fprintf(stderr, "%s: got %d, expected %d\n", description, actual,
            expected);
    return 1;
}

static int test_plant_ticks(void)
{
    struct plant_state state;
    struct plant_step_result result;

    plant_reset(&state);
    if (state.temperature_mC != 25000 || state.tick_index != 0U ||
        state.duty_q16_16 != 0 || state.safe != 1U) {
        fprintf(stderr, "plant reset state mismatch\n");
        return 1;
    }

    if (expect_status(plant_apply_duty(&state, 0), PLANT_STATUS_OK,
                       "zero duty") != 0) {
        return 1;
    }
    result = plant_step(&state);
    if (expect_status(result.status, PLANT_STATUS_OK, "tick zero") != 0 ||
        result.loss_mC != 0 || result.heater_mC != 0 ||
        result.disturbance_mC != 0 || result.candidate_temperature_mC != 25000 ||
        state.temperature_mC != 25000 || state.tick_index != 1U) {
        fprintf(stderr, "tick zero result mismatch\n");
        return 1;
    }

    plant_reset(&state);
    if (expect_status(plant_apply_duty(&state, 32768), PLANT_STATUS_OK,
                       "half duty") != 0) {
        return 1;
    }
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_OK || result.loss_mC != 0 ||
        result.heater_mC != 200 || state.temperature_mC != 25200) {
        fprintf(stderr, "half duty result mismatch\n");
        return 1;
    }

    plant_reset(&state);
    if (plant_apply_duty(&state, 65536) != PLANT_STATUS_OK) {
        return 1;
    }
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_OK || result.heater_mC != 400 ||
        state.temperature_mC != 25400) {
        fprintf(stderr, "full duty result mismatch\n");
        return 1;
    }

    plant_reset(&state);
    if (plant_apply_duty(&state, 2048) != PLANT_STATUS_OK) {
        return 1;
    }
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_OK || result.heater_mC != 13 ||
        state.temperature_mC != 25013) {
        fprintf(stderr, "half-up heater rounding mismatch\n");
        return 1;
    }

    state.temperature_mC = 26000;
    state.duty_q16_16 = 0;
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_OK || result.loss_mC != -10 ||
        state.temperature_mC != 25990) {
        fprintf(stderr, "negative loss truncation mismatch\n");
        return 1;
    }

    plant_reset(&state);
    state.tick_index = 599U;
    result = plant_step(&state);
    if (result.disturbance_mC != 0 || state.temperature_mC != 25000) {
        fprintf(stderr, "tick 599 disturbance mismatch\n");
        return 1;
    }
    result = plant_step(&state);
    if (result.disturbance_mC != -150 || state.temperature_mC != 24850) {
        fprintf(stderr, "tick 600 disturbance mismatch\n");
        return 1;
    }

    plant_reset(&state);
    state.tick_index = 899U;
    result = plant_step(&state);
    if (result.disturbance_mC != -150 || state.temperature_mC != 24850) {
        fprintf(stderr, "tick 899 disturbance mismatch\n");
        return 1;
    }
    state.temperature_mC = 25000;
    result = plant_step(&state);
    if (result.disturbance_mC != 0 || state.temperature_mC != 25000) {
        fprintf(stderr, "tick 900 disturbance mismatch\n");
        return 1;
    }

    plant_reset(&state);
    state.temperature_mC = PLANT_MAX_TEMPERATURE_MC;
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_OK || state.temperature_mC != 124000) {
        fprintf(stderr, "upper temperature boundary mismatch\n");
        return 1;
    }
    state.temperature_mC = PLANT_MIN_TEMPERATURE_MC;
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_OK || state.temperature_mC != -39350) {
        fprintf(stderr, "lower temperature boundary mismatch\n");
        return 1;
    }
    return 0;
}

static int test_plant_fail_closed(void)
{
    struct plant_state state;
    struct plant_step_result result;

    plant_reset(&state);
    if (plant_apply_duty(&state, -1) != PLANT_STATUS_INVALID_DUTY ||
        state.duty_q16_16 != 0 || state.safe != 1U) {
        fprintf(stderr, "negative duty was not rejected safely\n");
        return 1;
    }
    if (plant_apply_duty(&state, 65537) != PLANT_STATUS_INVALID_DUTY ||
        state.duty_q16_16 != 0 || state.safe != 1U) {
        fprintf(stderr, "over-range duty was not rejected safely\n");
        return 1;
    }

    state.temperature_mC = 200000;
    state.duty_q16_16 = 0;
    state.tick_index = 17U;
    result = plant_step(&state);
    if (result.status != PLANT_STATUS_RANGE_OVERFLOW ||
        result.candidate_temperature_mC != 200000 ||
        state.temperature_mC != 200000 || state.tick_index != 17U ||
        state.duty_q16_16 != 0 || state.safe != 1U) {
        fprintf(stderr, "plant overflow was not fail closed\n");
        return 1;
    }
    return 0;
}

static int test_fixed_pi_reference(void)
{
    struct plant_pi_controller controller;
    int32_t duty_q16_16 = -1;

    plant_pi_reset(&controller);
    if (plant_pi_update(&controller, 25000, 55000, &duty_q16_16) !=
            PLANT_STATUS_OK ||
        duty_q16_16 != 50135 || controller.integral_c_seconds != 3.0) {
        fprintf(stderr, "fixed PI first output mismatch\n");
        return 1;
    }

    controller.integral_c_seconds = 100.0;
    if (plant_pi_update(&controller, 100000, 55000, &duty_q16_16) !=
            PLANT_STATUS_OK ||
        duty_q16_16 != 0 || controller.integral_c_seconds != 100.0) {
        fprintf(stderr, "fixed PI anti-windup mismatch\n");
        return 1;
    }
    plant_pi_reset(&controller);
    if (controller.integral_c_seconds != 0.0) {
        fprintf(stderr, "fixed PI reset mismatch\n");
        return 1;
    }
    return 0;
}

static struct control_mailbox_command make_command(uint32_t session_id,
                                                    uint32_t sequence,
                                                    uint32_t request_id,
                                                    int32_t duty_q16_16)
{
    const struct control_mailbox_command command = {
        .session_id = session_id,
        .sequence = sequence,
        .request_id = request_id,
        .command = CONTROL_MAILBOX_APPLY_OUTPUT,
        .control_mode = CONTROL_MAILBOX_FIXED_BASELINE,
        .duty_q16_16 = duty_q16_16,
        .target_mC = CONTROL_MAILBOX_TARGET_MC,
        .model_version = 0U,
        .validity_ms = CONTROL_MAILBOX_VALIDITY_MS,
    };

    return command;
}

static int test_mailbox_boundaries(void)
{
    struct control_mailbox mailbox;
    struct control_mailbox_command command;
    struct control_mailbox_command received;

    control_mailbox_init(&mailbox);
    if (control_mailbox_begin_session(&mailbox, 7U) !=
        CONTROL_MAILBOX_STATUS_OK) {
        fprintf(stderr, "mailbox session start failed\n");
        return 1;
    }
    command = make_command(7U, 1U, 1U, 32768);
    if (control_mailbox_publish(&mailbox, &command) !=
            CONTROL_MAILBOX_STATUS_OK ||
        !control_mailbox_has_pending(&mailbox)) {
        fprintf(stderr, "mailbox first publish failed\n");
        return 1;
    }
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_DUPLICATE) {
        fprintf(stderr, "mailbox duplicate was accepted\n");
        return 1;
    }

    command = make_command(7U, 2U, 2U, 0);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_OK) {
        return 1;
    }
    command = make_command(7U, 3U, 3U, 65536);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_OK) {
        return 1;
    }
    if (control_mailbox_take(&mailbox, &received) !=
            CONTROL_MAILBOX_STATUS_OK ||
        received.sequence != 3U || received.request_id != 3U ||
        received.duty_q16_16 != 65536 ||
        control_mailbox_has_pending(&mailbox)) {
        fprintf(stderr, "mailbox latest-valid replacement mismatch\n");
        return 1;
    }
    if (control_mailbox_take(&mailbox, &received) !=
        CONTROL_MAILBOX_STATUS_EMPTY) {
        fprintf(stderr, "mailbox empty state mismatch\n");
        return 1;
    }

    command = make_command(7U, 2U, 2U, 0);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_STALE_SEQUENCE) {
        fprintf(stderr, "mailbox stale sequence was accepted\n");
        return 1;
    }
    command = make_command(7U, 4U, 2U, 0);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_STALE_REQUEST) {
        fprintf(stderr, "mailbox stale request was accepted\n");
        return 1;
    }
    command = make_command(7U, 4U, 4U, 0);
    command.validity_ms = 499U;
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_INVALID_VALIDITY) {
        fprintf(stderr, "mailbox validity 499 was accepted\n");
        return 1;
    }
    command.validity_ms = 501U;
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_INVALID_VALIDITY) {
        fprintf(stderr, "mailbox validity 501 was accepted\n");
        return 1;
    }

    command = make_command(7U, 4U, 4U, 0);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_OK) {
        fprintf(stderr, "mailbox valid command after rejection failed\n");
        return 1;
    }
    if (control_mailbox_begin_session(&mailbox, 8U) !=
        CONTROL_MAILBOX_STATUS_OK) {
        return 1;
    }
    command = make_command(7U, 5U, 5U, 0);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_SESSION_MISMATCH) {
        fprintf(stderr, "mailbox old session was accepted\n");
        return 1;
    }
    command = make_command(8U, 1U, 1U, 0);
    if (control_mailbox_publish(&mailbox, &command) !=
        CONTROL_MAILBOX_STATUS_OK) {
        fprintf(stderr, "mailbox new session command failed\n");
        return 1;
    }
    if (control_mailbox_begin_session(&mailbox, 7U) !=
        CONTROL_MAILBOX_STATUS_SESSION_NOT_NEW) {
        fprintf(stderr, "mailbox retired session was reused\n");
        return 1;
    }
    return 0;
}

int main(void)
{
    if (test_plant_ticks() != 0 || test_plant_fail_closed() != 0 ||
        test_fixed_pi_reference() != 0 || test_mailbox_boundaries() != 0) {
        return 1;
    }
    puts("PLANT_MAILBOX_HOST_PASS");
    return 0;
}
