/* P5-AI-A/B: deterministic C mirror of model.py:infer_duty_q16_16.
 *
 * All arithmetic is float32 to match the host numpy float32 reference within
 * the +-2 Q16.16 duty LSB contract.  Layout: W1[8][3], b1[8], W2[1][8], b2[1]
 * canonical little-endian float32 (41 floats, 164 bytes).
 */
#include "contest_model.h"
#include "contest_model_bin.h"
#include "contest_golden_vectors.h"
#include "controller.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

static float f32at(const unsigned char *p, size_t i) {
    /* little-endian float32 */
    unsigned char b[4];
    b[0] = p[4 * i];
    b[1] = p[4 * i + 1];
    b[2] = p[4 * i + 2];
    b[3] = p[4 * i + 3];
    float v;
    memcpy(&v, b, 4);
    return v;
}

static float clampf32(float x, float lo, float hi) {
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}

/* IF-008-v1 three-feature normalization in float32. */
static void normalize_inputs(int measured_mC, int target_mC,
                             int previous_duty_q16_16, float out[3]) {
    float v0 = (float)(measured_mC - 25000) / 40000.0f;
    float v1 = (float)(target_mC - 25000) / 40000.0f;
    float v2 = 2.0f * (float)previous_duty_q16_16 / 65536.0f - 1.0f;
    out[0] = clampf32(v0, -1.0f, 1.0f);
    out[1] = clampf32(v1, -1.0f, 1.0f);
    out[2] = clampf32(v2, -1.0f, 1.0f);
}

int contest_mlp_infer_duty(int measured_mC, int target_mC,
                           int previous_duty_q16_16) {
    if (sizeof(CONTEST_MODEL_BIN) != 164) return -1;

    float in[3];
    normalize_inputs(measured_mC, target_mC, previous_duty_q16_16, in);

    /* hidden[8] = relu(W1 * in + b1);  W1 row-major 8x3 (24 floats). */
    float hidden[8];
    for (int h = 0; h < 8; h++) {
        float acc = f32at(CONTEST_MODEL_BIN, 24 + h); /* b1[h] after W1 */
        acc += f32at(CONTEST_MODEL_BIN, h * 3 + 0) * in[0];
        acc += f32at(CONTEST_MODEL_BIN, h * 3 + 1) * in[1];
        acc += f32at(CONTEST_MODEL_BIN, h * 3 + 2) * in[2];
        hidden[h] = acc > 0.0f ? acc : 0.0f;
    }

    /* output = W2[1][8] * hidden + b2 ; W2 at index 32..39, b2 at index 40. */
    float output = f32at(CONTEST_MODEL_BIN, 40);
    for (int h = 0; h < 8; h++) {
        output += f32at(CONTEST_MODEL_BIN, 32 + h) * hidden[h];
    }

    /* sigmoid (float32) */
    float sig = 1.0f / (1.0f + expf(-output));
    float clamped = clampf32(sig, 0.0f, 1.0f);
    double scaled = (double)clamped * 65536.0 + 0.5;
    long rounded = (long)scaled; /* floor(* + 0.5) */
    if (rounded < 0) rounded = 0;
    if (rounded > 65536) rounded = 65536;
    return (int)rounded;
}

int contest_mlp_verify_golden(void) {
    int matched = 0;
    for (int i = 0; i < 256; i++) {
        const golden_vector_t *gv = &CONTEST_GOLDEN_VECTORS[i];
        int actual = contest_mlp_infer_duty(gv->in0, gv->in1, gv->in2);
        int diff = actual - gv->duty_q16_16;
        if (diff < 0) diff = -diff;
        if (diff <= 2) matched++;
    }
    return matched;
}

/* ---- P5-AI-B control planner + plant reference ---- */

int contest_control_duty(int measured_mC, int target_mC,
                         int previous_duty_q16_16) {
    int duty = contest_mlp_infer_duty(measured_mC, target_mC, previous_duty_q16_16);
    if (duty < CONTEST_MIN_DUTY) duty = CONTEST_MIN_DUTY;
    if (duty > CONTEST_MAX_DUTY) duty = CONTEST_MAX_DUTY;
    return duty;
}

int contest_plant_step(int temperature_mC, int duty_q16_16, int tick_index) {
    (void)tick_index;
    /* loss = trunc((AMBIENT - temp) / 100), C99-style toward zero */
    int loss = (CONTEST_AMBIENT_MC - temperature_mC) / CONTEST_LOSS_DIVISOR;
    if ((CONTEST_AMBIENT_MC - temperature_mC) < 0 &&
        (CONTEST_AMBIENT_MC - temperature_mC) % CONTEST_LOSS_DIVISOR != 0) {
        loss -= 1;
    }
    int heater = (CONTEST_HEATER_RATE_MCS / 10 * duty_q16_16 + 32768) / 65536;
    return temperature_mC + loss + heater;
}


/* ---- P5-AI-Q (TEST-017): frozen fixed-PI baseline ---- */

void contest_pi_reset(struct contest_pi_state *pi)
{
    if (pi != NULL) {
        pi->integral_c_seconds = 0.0;
    }
}

static double _clamp(double v, double lo, double hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

int contest_control_pi(struct contest_pi_state *pi, int measured_mC, int target_mC)
{
    if (pi == NULL) {
        return CONTEST_MIN_DUTY;
    }
    double e_C = (double)(target_mC - measured_mC) / 1000.0;
    double I = pi->integral_c_seconds;
    double I_cand = _clamp(I + 0.1 * e_C, -100.0, 100.0);
    double u_cand = 0.025 * e_C + 0.005 * I_cand;
    if ((u_cand > 1.0 && e_C > 0.0) || (u_cand < 0.0 && e_C < 0.0)) {
        /* anti-windup: reject the integral update, recompute with old I */
        u_cand = 0.025 * e_C + 0.005 * I;
    } else {
        pi->integral_c_seconds = I_cand;
    }
    double u = _clamp(u_cand, 0.0, 1.0);
    int duty = (int)(u * 65536.0 + 0.5); /* nearest, half up */
    if (duty < CONTEST_MIN_DUTY) duty = CONTEST_MIN_DUTY;
    if (duty > CONTEST_MAX_DUTY) duty = CONTEST_MAX_DUTY;
    return duty;
}
