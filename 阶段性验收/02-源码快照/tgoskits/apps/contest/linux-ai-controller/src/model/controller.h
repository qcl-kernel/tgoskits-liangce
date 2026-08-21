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

int contest_control_duty(int measured_mC, int target_mC, int previous_duty_q16_16);
int contest_plant_step(int temperature_mC, int duty_q16_16, int tick_index);

/* P5-AI-Q (TEST-017): frozen fixed-PI baseline (IF-008 / DEC-008).
 * e_C = (target - measured)/1000; Kp = 0.025 duty/C; Ki = 0.005 duty/(C*s);
 * integral clamp [-100,100] C*s with the anti-windup rejection rule; output
 * clamp [0,1] then Q16.16 nearest (half up).  Integral resets on SAFE /
 * target change / session change (caller resets via contest_pi_reset). */
struct contest_pi_state {
    double integral_c_seconds;
};
void contest_pi_reset(struct contest_pi_state *pi);
int contest_control_pi(struct contest_pi_state *pi, int measured_mC, int target_mC);

#endif /* CONTEST_CONTROLLER_H */
