#include <stddef.h>
#include "plant.h"

#include <math.h>

static int valid_duty(int32_t duty_q16_16);
static int64_t plant_loss(int32_t temperature_mC);
static int64_t plant_heater(int32_t duty_q16_16);
static int64_t plant_disturbance(uint64_t tick_index);
static double clamp_integral(double integral_c_seconds);
static double fixed_pi_output(double error_c, double integral_c_seconds);
static int32_t quantize_pi_output(double output);

void plant_reset(struct plant_state *state)
{
    if (state == NULL) {
        return;
    }

    state->temperature_mC = PLANT_INITIAL_TEMPERATURE_MC;
    state->tick_index = 0U;
    state->duty_q16_16 = PLANT_MIN_DUTY_Q16_16;
    state->safe = UINT8_C(1);
}

void plant_enter_safe(struct plant_state *state)
{
    if (state == NULL) {
        return;
    }

    state->duty_q16_16 = PLANT_MIN_DUTY_Q16_16;
    state->safe = UINT8_C(1);
}

enum plant_status plant_apply_duty(struct plant_state *state,
                                   int32_t duty_q16_16)
{
    if (state == NULL) {
        return PLANT_STATUS_NULL_ARGUMENT;
    }
    if (!valid_duty(duty_q16_16)) {
        plant_enter_safe(state);
        return PLANT_STATUS_INVALID_DUTY;
    }

    state->duty_q16_16 = duty_q16_16;
    state->safe = UINT8_C(0);
    return PLANT_STATUS_OK;
}

struct plant_step_result plant_step(struct plant_state *state)
{
    struct plant_step_result result = {
        .status = PLANT_STATUS_OK,
        .candidate_temperature_mC = 0,
        .loss_mC = 0,
        .heater_mC = 0,
        .disturbance_mC = 0,
    };
    int64_t next_temperature_mC;

    if (state == NULL) {
        result.status = PLANT_STATUS_NULL_ARGUMENT;
        return result;
    }
    if (!valid_duty(state->duty_q16_16)) {
        plant_enter_safe(state);
        result.status = PLANT_STATUS_INVALID_DUTY;
        return result;
    }
    if (state->temperature_mC < PLANT_MIN_TEMPERATURE_MC ||
        state->temperature_mC > PLANT_MAX_TEMPERATURE_MC) {
        plant_enter_safe(state);
        result.status = PLANT_STATUS_RANGE_OVERFLOW;
        result.candidate_temperature_mC = state->temperature_mC;
        return result;
    }
    if (state->tick_index == UINT64_MAX) {
        plant_enter_safe(state);
        result.status = PLANT_STATUS_TICK_OVERFLOW;
        result.candidate_temperature_mC = state->temperature_mC;
        return result;
    }

    /* C99 signed division deliberately supplies truncation toward zero. */
    result.loss_mC = (int32_t)plant_loss(state->temperature_mC);
    result.heater_mC = (int32_t)plant_heater(state->duty_q16_16);
    result.disturbance_mC = (int32_t)plant_disturbance(state->tick_index);
    next_temperature_mC = (int64_t)state->temperature_mC +
                          (int64_t)result.loss_mC +
                          (int64_t)result.heater_mC +
                          (int64_t)result.disturbance_mC;
    result.candidate_temperature_mC = next_temperature_mC;

    if (next_temperature_mC < PLANT_MIN_TEMPERATURE_MC ||
        next_temperature_mC > PLANT_MAX_TEMPERATURE_MC) {
        plant_enter_safe(state);
        result.status = PLANT_STATUS_RANGE_OVERFLOW;
        return result;
    }

    state->temperature_mC = (int32_t)next_temperature_mC;
    state->tick_index += UINT64_C(1);
    return result;
}

void plant_pi_reset(struct plant_pi_controller *controller)
{
    if (controller == NULL) {
        return;
    }

    controller->integral_c_seconds = 0.0;
}

enum plant_status plant_pi_update(struct plant_pi_controller *controller,
                                  int32_t measured_mC, int32_t target_mC,
                                  int32_t *duty_q16_16)
{
    double error_c;
    double previous_integral;
    double candidate_integral;
    double candidate_output;
    double output;

    if (controller == NULL || duty_q16_16 == NULL) {
        return PLANT_STATUS_NULL_ARGUMENT;
    }

    previous_integral = controller->integral_c_seconds;
    if (!isfinite(previous_integral)) {
        return PLANT_STATUS_NONFINITE_PI;
    }

    error_c = ((double)target_mC - (double)measured_mC) / 1000.0;
    candidate_integral = clamp_integral(previous_integral + 0.1 * error_c);
    candidate_output = fixed_pi_output(error_c, candidate_integral);

    if ((candidate_output > 1.0 && error_c > 0.0) ||
        (candidate_output < 0.0 && error_c < 0.0)) {
        controller->integral_c_seconds = previous_integral;
        output = fixed_pi_output(error_c, previous_integral);
    } else {
        controller->integral_c_seconds = candidate_integral;
        output = candidate_output;
    }

    if (!isfinite(output)) {
        return PLANT_STATUS_NONFINITE_PI;
    }
    if (output < 0.0) {
        output = 0.0;
    } else if (output > 1.0) {
        output = 1.0;
    }
    *duty_q16_16 = quantize_pi_output(output);
    return PLANT_STATUS_OK;
}

static int valid_duty(int32_t duty_q16_16)
{
    return duty_q16_16 >= PLANT_MIN_DUTY_Q16_16 &&
           duty_q16_16 <= PLANT_MAX_DUTY_Q16_16;
}

static int64_t plant_loss(int32_t temperature_mC)
{
    const int64_t numerator = (int64_t)PLANT_AMBIENT_TEMPERATURE_MC -
                              (int64_t)temperature_mC;

    return numerator / PLANT_HEAT_LOSS_DIVISOR;
}

static int64_t plant_heater(int32_t duty_q16_16)
{
    const int64_t numerator = (int64_t)PLANT_HEATER_RATE_MC_PER_SECOND / 10 *
                              (int64_t)duty_q16_16 +
                              INT64_C(32768);

    return numerator / INT64_C(65536);
}

static int64_t plant_disturbance(uint64_t tick_index)
{
    if (tick_index >= PLANT_DISTURBANCE_START_TICK &&
        tick_index <= PLANT_DISTURBANCE_END_TICK) {
        return PLANT_DISTURBANCE_MC_PER_TICK;
    }
    return 0;
}

static double clamp_integral(double integral_c_seconds)
{
    if (integral_c_seconds < -100.0) {
        return -100.0;
    }
    if (integral_c_seconds > 100.0) {
        return 100.0;
    }
    return integral_c_seconds;
}

static double fixed_pi_output(double error_c, double integral_c_seconds)
{
    const double proportional_term = 0.025 * error_c;
    const double integral_term = 0.005 * integral_c_seconds;

    return proportional_term + integral_term;
}

static int32_t quantize_pi_output(double output)
{
    if (output <= 0.0) {
        return PLANT_MIN_DUTY_Q16_16;
    }
    if (output >= 1.0) {
        return PLANT_MAX_DUTY_Q16_16;
    }
    return (int32_t)(output * 65536.0 + 0.5);
}
