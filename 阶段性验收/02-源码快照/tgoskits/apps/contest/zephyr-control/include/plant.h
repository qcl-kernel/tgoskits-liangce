#ifndef CONTEST_ZEPHYR_CONTROL_PLANT_H
#define CONTEST_ZEPHYR_CONTROL_PLANT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PLANT_TICK_PERIOD_MS UINT32_C(100)
#define PLANT_INITIAL_TEMPERATURE_MC INT32_C(25000)
#define PLANT_AMBIENT_TEMPERATURE_MC INT32_C(25000)
#define PLANT_TARGET_TEMPERATURE_MC INT32_C(55000)
#define PLANT_HEATER_RATE_MC_PER_SECOND INT32_C(4000)
#define PLANT_HEAT_LOSS_DIVISOR INT32_C(100)
#define PLANT_MIN_TEMPERATURE_MC INT32_C(-40000)
#define PLANT_MAX_TEMPERATURE_MC INT32_C(125000)
#define PLANT_DISTURBANCE_START_TICK UINT64_C(600)
#define PLANT_DISTURBANCE_END_TICK UINT64_C(899)
#define PLANT_DISTURBANCE_MC_PER_TICK INT32_C(-150)
#define PLANT_MIN_DUTY_Q16_16 INT32_C(0)
#define PLANT_MAX_DUTY_Q16_16 INT32_C(65536)

enum plant_status {
    PLANT_STATUS_OK = 0,
    PLANT_STATUS_NULL_ARGUMENT,
    PLANT_STATUS_INVALID_DUTY,
    PLANT_STATUS_RANGE_OVERFLOW,
    PLANT_STATUS_TICK_OVERFLOW,
    PLANT_STATUS_NONFINITE_PI,
};

struct plant_state {
    int32_t temperature_mC;
    uint64_t tick_index;
    int32_t duty_q16_16;
    uint8_t safe;
};

struct plant_step_result {
    enum plant_status status;
    int64_t candidate_temperature_mC;
    int32_t loss_mC;
    int32_t heater_mC;
    int32_t disturbance_mC;
};

struct plant_pi_controller {
    double integral_c_seconds;
};

/**
 * Reset a plant to the frozen qualification initial state.
 *
 * A null state is ignored so that a teardown path cannot turn a safety reset
 * into a host crash.
 */
void plant_reset(struct plant_state *state);

/**
 * Force the plant actuator to the safe value without changing temperature or
 * tick state.
 */
void plant_enter_safe(struct plant_state *state);

/**
 * Apply one validated duty at a period boundary.
 *
 * Invalid duty values fail closed by forcing duty zero and safe state. The
 * plant temperature and tick are not changed by a rejected duty.
 */
enum plant_status plant_apply_duty(struct plant_state *state,
                                   int32_t duty_q16_16);

/**
 * Advance the plant by one 100 ms tick using the duty selected at the period
 * boundary.
 *
 * The result is committed only when it is in the inclusive mC range
 * [-40000, 125000]. An out-of-range result leaves temperature and tick
 * unchanged, then forces duty zero and safe state.
 */
struct plant_step_result plant_step(struct plant_state *state);

/**
 * Reset the fixed binary64 PI controller integral to zero.
 */
void plant_pi_reset(struct plant_pi_controller *controller);

/**
 * Compute one fixed PI Q16.16 output using the frozen anti-windup rule.
 *
 * The controller uses the expression order from the P5 contract and rounds
 * non-negative output with floor(u * 65536 + 0.5).
 */
enum plant_status plant_pi_update(struct plant_pi_controller *controller,
                                  int32_t measured_mC, int32_t target_mC,
                                  int32_t *duty_q16_16);

#ifdef __cplusplus
}
#endif

#endif
