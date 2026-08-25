#ifndef ICPC_CONTROL_H
#define ICPC_CONTROL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ICPC_CONTROL_SCHEMA_VERSION 1U
#define ICPC_CONTROL_PAYLOAD_SIZE 24U
#define ICPC_STATUS_PAYLOAD_SIZE 32U
#define ICPC_ERROR_PAYLOAD_SIZE 12U
#define ICPC_CONTROL_TARGET_MILLI_CELSIUS 55000
#define ICPC_CONTROL_VALIDITY_MS 500U
#define ICPC_CONTROL_MIN_DUTY_Q16_16 0
#define ICPC_CONTROL_MAX_DUTY_Q16_16 65536

enum icpc_control_command {
    ICPC_CONTROL_APPLY_OUTPUT = 1,
    ICPC_CONTROL_ENTER_SAFE = 2,
    ICPC_CONTROL_SET_TARGET = 3,
};

enum icpc_control_mode {
    ICPC_CONTROL_FIXED_BASELINE = 0,
    ICPC_CONTROL_MLP = 1,
};

enum icpc_health_flag {
    ICPC_HEALTH_SAFE = 1U << 0,
    ICPC_HEALTH_NETWORK_TIMEOUT = 1U << 1,
    ICPC_HEALTH_SENSOR_INVALID = 1U << 2,
    ICPC_HEALTH_ACTUATOR_CLAMPED = 1U << 3,
    ICPC_HEALTH_DEADLINE_MISS = 1U << 4,
};

enum icpc_error_subsystem {
    ICPC_SUBSYSTEM_PROTOCOL = 1,
    ICPC_SUBSYSTEM_NETWORK = 2,
    ICPC_SUBSYSTEM_CONTROL = 3,
    ICPC_SUBSYSTEM_MODEL = 4,
    ICPC_SUBSYSTEM_RUNTIME = 5,
};

enum icpc_control_status {
    ICPC_CONTROL_STATUS_OK = 0,
    ICPC_CONTROL_STATUS_NULL_ARGUMENT,
    ICPC_CONTROL_STATUS_INVALID_LENGTH,
    ICPC_CONTROL_STATUS_INVALID_SCHEMA,
    ICPC_CONTROL_STATUS_INVALID_COMMAND,
    ICPC_CONTROL_STATUS_INVALID_MODE,
    ICPC_CONTROL_STATUS_INVALID_RESERVED,
    ICPC_CONTROL_STATUS_INVALID_REQUEST,
    ICPC_CONTROL_STATUS_INVALID_DUTY,
    ICPC_CONTROL_STATUS_INVALID_TARGET,
    ICPC_CONTROL_STATUS_INVALID_MODEL_VERSION,
    ICPC_CONTROL_STATUS_INVALID_VALIDITY,
    ICPC_CONTROL_STATUS_INVALID_HEALTH_FLAGS,
    ICPC_CONTROL_STATUS_INVALID_SUBSYSTEM,
    ICPC_CONTROL_STATUS_INVALID_DETAIL,
};

struct icpc_control_payload {
    uint8_t schema_version;
    uint8_t command;
    uint8_t control_mode;
    uint32_t request_id;
    int32_t duty_q16_16;
    int32_t target_mC;
    uint32_t model_version;
    uint16_t validity_ms;
};

struct icpc_status_payload {
    uint8_t schema_version;
    uint8_t control_mode;
    uint16_t health_flags;
    uint32_t applied_request_id;
    uint64_t sample_index;
    int32_t measured_mC;
    int32_t target_mC;
    int32_t duty_q16_16;
    int32_t control_error_mC;
};

struct icpc_error_payload {
    uint8_t schema_version;
    uint8_t subsystem;
    uint16_t detail_code;
    uint32_t related_request_id;
    uint32_t diagnostic_token;
};

enum icpc_control_status
icpc_control_encode(uint8_t *buffer, size_t capacity,
                    const struct icpc_control_payload *payload,
                    size_t *encoded_length);

enum icpc_control_status
icpc_control_decode(const uint8_t *buffer, size_t length,
                    struct icpc_control_payload *payload);

enum icpc_control_status
icpc_status_encode(uint8_t *buffer, size_t capacity,
                   const struct icpc_status_payload *payload,
                   size_t *encoded_length);

enum icpc_control_status
icpc_status_decode(const uint8_t *buffer, size_t length,
                   struct icpc_status_payload *payload);

enum icpc_control_status
icpc_error_encode(uint8_t *buffer, size_t capacity,
                  const struct icpc_error_payload *payload,
                  size_t *encoded_length);

enum icpc_control_status
icpc_error_decode(const uint8_t *buffer, size_t length,
                  struct icpc_error_payload *payload);

#ifdef __cplusplus
}
#endif

#endif
