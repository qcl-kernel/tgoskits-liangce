#ifndef CONTEST_MODEL_H
#define CONTEST_MODEL_H

#include <stdint.h>

/* Frozen P5-AI-A canonical model: 3x8x1 float32 MLP (ReLU hidden, sigmoid
 * output), IF-008-v1 normalization, Q16.16 duty quantization (0..65536).
 * contest_mlp_infer_duty mirrors model.py:infer_duty_q16_16 within +-2 LSB. */
int contest_mlp_infer_duty(int measured_mC, int target_mC,
                           int previous_duty_q16_16);

/* Runs every frozen golden vector and returns how many matched within 2 LSB. */
int contest_mlp_verify_golden(void);

#endif /* CONTEST_MODEL_H */
