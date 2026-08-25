#include "icpc_control.h"

static void write_u16_be(uint8_t *output, uint16_t value);
static void write_u32_be(uint8_t *output, uint32_t value);
static void write_u64_be(uint8_t *output, uint64_t value);
static uint16_t read_u16_be(const uint8_t *input);
static uint32_t read_u32_be(const uint8_t *input);
static uint64_t read_u64_be(const uint8_t *input);
static void write_i32_be(uint8_t *output, int32_t value);
static int32_t read_i32_be(const uint8_t *input);
static enum icpc_control_status validate_control(
    const struct icpc_control_payload *payload);
static enum icpc_control_status validate_status(
    const struct icpc_status_payload *payload);
static enum icpc_control_status validate_error(
    const struct icpc_error_payload *payload);
static int valid_command(uint8_t command);
static int valid_mode(uint8_t mode);

enum icpc_control_status
icpc_control_encode(uint8_t *buffer, size_t capacity,
                    const struct icpc_control_payload *payload,
                    size_t *encoded_length)
{
    enum icpc_control_status status;

    if (buffer == NULL || payload == NULL || encoded_length == NULL) {
        return ICPC_CONTROL_STATUS_NULL_ARGUMENT;
    }
    *encoded_length = 0U;
    if (capacity < ICPC_CONTROL_PAYLOAD_SIZE) {
        return ICPC_CONTROL_STATUS_INVALID_LENGTH;
    }
    status = validate_control(payload);
    if (status != ICPC_CONTROL_STATUS_OK) {
        return status;
    }

    buffer[0] = payload->schema_version;
    buffer[1] = payload->command;
    buffer[2] = payload->control_mode;
    buffer[3] = 0U;
    write_u32_be(&buffer[4], payload->request_id);
    write_i32_be(&buffer[8], payload->duty_q16_16);
    write_i32_be(&buffer[12], payload->target_mC);
    write_u32_be(&buffer[16], payload->model_version);
    write_u16_be(&buffer[20], payload->validity_ms);
    write_u16_be(&buffer[22], 0U);
    *encoded_length = ICPC_CONTROL_PAYLOAD_SIZE;
    return ICPC_CONTROL_STATUS_OK;
}

enum icpc_control_status
icpc_control_decode(const uint8_t *buffer, size_t length,
                    struct icpc_control_payload *payload)
{
    enum icpc_control_status status;

    if (buffer == NULL || payload == NULL) {
        return ICPC_CONTROL_STATUS_NULL_ARGUMENT;
    }
    if (length != ICPC_CONTROL_PAYLOAD_SIZE) {
        return ICPC_CONTROL_STATUS_INVALID_LENGTH;
    }
    if (buffer[3] != 0U || read_u16_be(&buffer[22]) != 0U) {
        return ICPC_CONTROL_STATUS_INVALID_RESERVED;
    }

    payload->schema_version = buffer[0];
    payload->command = buffer[1];
    payload->control_mode = buffer[2];
    payload->request_id = read_u32_be(&buffer[4]);
    payload->duty_q16_16 = read_i32_be(&buffer[8]);
    payload->target_mC = read_i32_be(&buffer[12]);
    payload->model_version = read_u32_be(&buffer[16]);
    payload->validity_ms = read_u16_be(&buffer[20]);
    status = validate_control(payload);
    return status;
}

enum icpc_control_status
icpc_status_encode(uint8_t *buffer, size_t capacity,
                   const struct icpc_status_payload *payload,
                   size_t *encoded_length)
{
    enum icpc_control_status status;

    if (buffer == NULL || payload == NULL || encoded_length == NULL) {
        return ICPC_CONTROL_STATUS_NULL_ARGUMENT;
    }
    *encoded_length = 0U;
    if (capacity < ICPC_STATUS_PAYLOAD_SIZE) {
        return ICPC_CONTROL_STATUS_INVALID_LENGTH;
    }
    status = validate_status(payload);
    if (status != ICPC_CONTROL_STATUS_OK) {
        return status;
    }

    buffer[0] = payload->schema_version;
    buffer[1] = payload->control_mode;
    write_u16_be(&buffer[2], payload->health_flags);
    write_u32_be(&buffer[4], payload->applied_request_id);
    write_u64_be(&buffer[8], payload->sample_index);
    write_i32_be(&buffer[16], payload->measured_mC);
    write_i32_be(&buffer[20], payload->target_mC);
    write_i32_be(&buffer[24], payload->duty_q16_16);
    write_i32_be(&buffer[28], payload->control_error_mC);
    *encoded_length = ICPC_STATUS_PAYLOAD_SIZE;
    return ICPC_CONTROL_STATUS_OK;
}

enum icpc_control_status
icpc_status_decode(const uint8_t *buffer, size_t length,
                   struct icpc_status_payload *payload)
{
    enum icpc_control_status status;

    if (buffer == NULL || payload == NULL) {
        return ICPC_CONTROL_STATUS_NULL_ARGUMENT;
    }
    if (length != ICPC_STATUS_PAYLOAD_SIZE) {
        return ICPC_CONTROL_STATUS_INVALID_LENGTH;
    }

    payload->schema_version = buffer[0];
    payload->control_mode = buffer[1];
    payload->health_flags = read_u16_be(&buffer[2]);
    payload->applied_request_id = read_u32_be(&buffer[4]);
    payload->sample_index = read_u64_be(&buffer[8]);
    payload->measured_mC = read_i32_be(&buffer[16]);
    payload->target_mC = read_i32_be(&buffer[20]);
    payload->duty_q16_16 = read_i32_be(&buffer[24]);
    payload->control_error_mC = read_i32_be(&buffer[28]);
    status = validate_status(payload);
    return status;
}

enum icpc_control_status
icpc_error_encode(uint8_t *buffer, size_t capacity,
                  const struct icpc_error_payload *payload,
                  size_t *encoded_length)
{
    enum icpc_control_status status;

    if (buffer == NULL || payload == NULL || encoded_length == NULL) {
        return ICPC_CONTROL_STATUS_NULL_ARGUMENT;
    }
    *encoded_length = 0U;
    if (capacity < ICPC_ERROR_PAYLOAD_SIZE) {
        return ICPC_CONTROL_STATUS_INVALID_LENGTH;
    }
    status = validate_error(payload);
    if (status != ICPC_CONTROL_STATUS_OK) {
        return status;
    }

    buffer[0] = payload->schema_version;
    buffer[1] = payload->subsystem;
    write_u16_be(&buffer[2], payload->detail_code);
    write_u32_be(&buffer[4], payload->related_request_id);
    write_u32_be(&buffer[8], payload->diagnostic_token);
    *encoded_length = ICPC_ERROR_PAYLOAD_SIZE;
    return ICPC_CONTROL_STATUS_OK;
}

enum icpc_control_status
icpc_error_decode(const uint8_t *buffer, size_t length,
                  struct icpc_error_payload *payload)
{
    if (buffer == NULL || payload == NULL) {
        return ICPC_CONTROL_STATUS_NULL_ARGUMENT;
    }
    if (length != ICPC_ERROR_PAYLOAD_SIZE) {
        return ICPC_CONTROL_STATUS_INVALID_LENGTH;
    }

    payload->schema_version = buffer[0];
    payload->subsystem = buffer[1];
    payload->detail_code = read_u16_be(&buffer[2]);
    payload->related_request_id = read_u32_be(&buffer[4]);
    payload->diagnostic_token = read_u32_be(&buffer[8]);
    return validate_error(payload);
}

static enum icpc_control_status validate_control(
    const struct icpc_control_payload *payload)
{
    if (payload->schema_version != ICPC_CONTROL_SCHEMA_VERSION) {
        return ICPC_CONTROL_STATUS_INVALID_SCHEMA;
    }
    if (!valid_command(payload->command)) {
        return ICPC_CONTROL_STATUS_INVALID_COMMAND;
    }
    if (!valid_mode(payload->control_mode)) {
        return ICPC_CONTROL_STATUS_INVALID_MODE;
    }
    if (payload->request_id == 0U) {
        return ICPC_CONTROL_STATUS_INVALID_REQUEST;
    }
    if (payload->target_mC != ICPC_CONTROL_TARGET_MILLI_CELSIUS) {
        return ICPC_CONTROL_STATUS_INVALID_TARGET;
    }
    if (payload->validity_ms != ICPC_CONTROL_VALIDITY_MS) {
        return ICPC_CONTROL_STATUS_INVALID_VALIDITY;
    }

    if (payload->command == ICPC_CONTROL_APPLY_OUTPUT) {
        if (payload->duty_q16_16 < ICPC_CONTROL_MIN_DUTY_Q16_16 ||
            payload->duty_q16_16 > ICPC_CONTROL_MAX_DUTY_Q16_16) {
            return ICPC_CONTROL_STATUS_INVALID_DUTY;
        }
        if (payload->control_mode == ICPC_CONTROL_MLP) {
            if (payload->model_version == 0U) {
                return ICPC_CONTROL_STATUS_INVALID_MODEL_VERSION;
            }
        } else if (payload->model_version != 0U) {
            return ICPC_CONTROL_STATUS_INVALID_MODEL_VERSION;
        }
        return ICPC_CONTROL_STATUS_OK;
    }

    if (payload->duty_q16_16 != 0 || payload->model_version != 0U) {
        return ICPC_CONTROL_STATUS_INVALID_DUTY;
    }
    return ICPC_CONTROL_STATUS_OK;
}

static enum icpc_control_status validate_status(
    const struct icpc_status_payload *payload)
{
    const uint16_t known_flags = ICPC_HEALTH_SAFE |
                                 ICPC_HEALTH_NETWORK_TIMEOUT |
                                 ICPC_HEALTH_SENSOR_INVALID |
                                 ICPC_HEALTH_ACTUATOR_CLAMPED |
                                 ICPC_HEALTH_DEADLINE_MISS;

    if (payload->schema_version != ICPC_CONTROL_SCHEMA_VERSION) {
        return ICPC_CONTROL_STATUS_INVALID_SCHEMA;
    }
    if (!valid_mode(payload->control_mode)) {
        return ICPC_CONTROL_STATUS_INVALID_MODE;
    }
    if ((payload->health_flags & (uint16_t)~known_flags) != 0U) {
        return ICPC_CONTROL_STATUS_INVALID_HEALTH_FLAGS;
    }
    if (payload->target_mC != ICPC_CONTROL_TARGET_MILLI_CELSIUS) {
        return ICPC_CONTROL_STATUS_INVALID_TARGET;
    }
    if (payload->duty_q16_16 < ICPC_CONTROL_MIN_DUTY_Q16_16 ||
        payload->duty_q16_16 > ICPC_CONTROL_MAX_DUTY_Q16_16) {
        return ICPC_CONTROL_STATUS_INVALID_DUTY;
    }
    return ICPC_CONTROL_STATUS_OK;
}

static enum icpc_control_status validate_error(
    const struct icpc_error_payload *payload)
{
    if (payload->schema_version != ICPC_CONTROL_SCHEMA_VERSION) {
        return ICPC_CONTROL_STATUS_INVALID_SCHEMA;
    }
    if (payload->subsystem < ICPC_SUBSYSTEM_PROTOCOL ||
        payload->subsystem > ICPC_SUBSYSTEM_RUNTIME) {
        return ICPC_CONTROL_STATUS_INVALID_SUBSYSTEM;
    }
    if (payload->detail_code == 0U) {
        return ICPC_CONTROL_STATUS_INVALID_DETAIL;
    }
    return ICPC_CONTROL_STATUS_OK;
}

static int valid_command(uint8_t command)
{
    return command >= ICPC_CONTROL_APPLY_OUTPUT &&
           command <= ICPC_CONTROL_SET_TARGET;
}

static int valid_mode(uint8_t mode)
{
    return mode == ICPC_CONTROL_FIXED_BASELINE || mode == ICPC_CONTROL_MLP;
}

static void write_u16_be(uint8_t *output, uint16_t value)
{
    output[0] = (uint8_t)(value >> 8U);
    output[1] = (uint8_t)value;
}

static void write_u32_be(uint8_t *output, uint32_t value)
{
    output[0] = (uint8_t)(value >> 24U);
    output[1] = (uint8_t)(value >> 16U);
    output[2] = (uint8_t)(value >> 8U);
    output[3] = (uint8_t)value;
}

static void write_u64_be(uint8_t *output, uint64_t value)
{
    output[0] = (uint8_t)(value >> 56U);
    output[1] = (uint8_t)(value >> 48U);
    output[2] = (uint8_t)(value >> 40U);
    output[3] = (uint8_t)(value >> 32U);
    output[4] = (uint8_t)(value >> 24U);
    output[5] = (uint8_t)(value >> 16U);
    output[6] = (uint8_t)(value >> 8U);
    output[7] = (uint8_t)value;
}

static uint16_t read_u16_be(const uint8_t *input)
{
    return (uint16_t)(((uint16_t)input[0] << 8U) | input[1]);
}

static uint32_t read_u32_be(const uint8_t *input)
{
    return ((uint32_t)input[0] << 24U) | ((uint32_t)input[1] << 16U) |
           ((uint32_t)input[2] << 8U) | (uint32_t)input[3];
}

static uint64_t read_u64_be(const uint8_t *input)
{
    return ((uint64_t)input[0] << 56U) | ((uint64_t)input[1] << 48U) |
           ((uint64_t)input[2] << 40U) | ((uint64_t)input[3] << 32U) |
           ((uint64_t)input[4] << 24U) | ((uint64_t)input[5] << 16U) |
           ((uint64_t)input[6] << 8U) | (uint64_t)input[7];
}

static void write_i32_be(uint8_t *output, int32_t value)
{
    write_u32_be(output, (uint32_t)value);
}

static int32_t read_i32_be(const uint8_t *input)
{
    return (int32_t)read_u32_be(input);
}
