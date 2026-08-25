#ifndef CONTEST_CONTROLLER_H
#define CONTEST_CONTROLLER_H

/* P5-AI-B: Linux-side MLP control planner + deterministic plant reference.
 *
 * contest_control_duty() maps the frozen plant state to a Q16.16 actuator duty
 * using the canonical 3x8x1 MLP (contest_mlp_infer_duty), mirroring the P5
 * fixed-baseline/MLP contract (DEC-008).  Always in [0,65536].
 *
 * contest_plant_step() advances the DEC-008 one-tick temperature plant
 * (HEATER 4000 mC/s, loss divisor 100, ambient 25000 mC; 100 ms ticks).
 * It is the Linux-side reference used by host tests and by the CONTROL
 * generator when no Zephyr STATUS has arrived yet.
 */
#define CONTEST_TARGET_MC 55000
#define CONTEST_AMBIENT_MC 25000
#define CONTEST_HEATER_RATE_MCS 4000
#define CONTEST_LOSS_DIVISOR 100
#define CONTEST_TICK_MS 100
#define CONTEST_MIN_DUTY 0
#define CONTEST_MAX_DUTY 65536

struct contest_fixed_pi_state {
    double integral_c_seconds;
    int target_mC;
    unsigned char initialized;
};

int contest_control_duty(int measured_mC, int target_mC, int previous_duty_q16_16);
void contest_fixed_pi_reset(struct contest_fixed_pi_state *state);
int contest_fixed_pi_update(struct contest_fixed_pi_state *state,
                            int measured_mC, int target_mC);
int contest_plant_step(int temperature_mC, int duty_q16_16, int tick_index);

#endif /* CONTEST_CONTROLLER_H */
