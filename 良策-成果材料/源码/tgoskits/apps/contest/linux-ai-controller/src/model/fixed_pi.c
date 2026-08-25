/* P5 fixed PI baseline, compatible with the canonical Python reference. */

#include "controller.h"

#include <math.h>
#include <stddef.h>

static double clamp_double(double value, double lower, double upper)
{
    if (value < lower) {
        return lower;
    }
    if (value > upper) {
        return upper;
    }
    return value;
}

void contest_fixed_pi_reset(struct contest_fixed_pi_state *state)
{
    if (state == NULL) {
        return;
    }
    state->integral_c_seconds = 0.0;
    state->target_mC = 0;
    state->initialized = 0U;
}

int contest_fixed_pi_update(struct contest_fixed_pi_state *state,
                            int measured_mC, int target_mC)
{
    double error_c;
    double previous_integral;
    double candidate_integral;
    double candidate_output;
    double output;

    if (state == NULL) {
        return -1;
    }
    if (state->initialized == 0U || state->target_mC != target_mC) {
        state->integral_c_seconds = 0.0;
        state->target_mC = target_mC;
        state->initialized = 1U;
    }
    error_c = ((double)target_mC - (double)measured_mC) / 1000.0;
    previous_integral = state->integral_c_seconds;
    candidate_integral = clamp_double(
        previous_integral + 0.1 * error_c, -100.0, 100.0);
    candidate_output = 0.025 * error_c + 0.005 * candidate_integral;
    if ((candidate_output > 1.0 && error_c > 0.0) ||
        (candidate_output < 0.0 && error_c < 0.0)) {
        state->integral_c_seconds = previous_integral;
        output = 0.025 * error_c + 0.005 * previous_integral;
    } else {
        state->integral_c_seconds = candidate_integral;
        output = candidate_output;
    }
    output = clamp_double(output, 0.0, 1.0);
    return (int)floor(output * 65536.0 + 0.5);
}
