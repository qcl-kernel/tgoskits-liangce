#include "icpc_control.h"

#include <stdio.h>
#include <string.h>

static int expect_status(enum icpc_control_status actual,
                         enum icpc_control_status expected,
                         const char *description)
{
    if (actual == expected) {
        return 0;
    }
    fprintf(stderr, "%s: got %d, expected %d\n", description, actual,
            expected);
    return 1;
}

static int test_control_round_trip(void)
{
    static const uint8_t expected[ICPC_CONTROL_PAYLOAD_SIZE] = {
        0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x2a,
        0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0xd6, 0xd8,
        0x01, 0x02, 0x03, 0x04, 0x01, 0xf4, 0x00, 0x00,
    };
    const struct icpc_control_payload source = {
        .schema_version = ICPC_CONTROL_SCHEMA_VERSION,
        .command = ICPC_CONTROL_APPLY_OUTPUT,
        .control_mode = ICPC_CONTROL_MLP,
        .request_id = 42U,
        .duty_q16_16 = 65536,
        .target_mC = ICPC_CONTROL_TARGET_MILLI_CELSIUS,
        .model_version = UINT32_C(0x01020304),
        .validity_ms = ICPC_CONTROL_VALIDITY_MS,
    };
    struct icpc_control_payload decoded;
    uint8_t encoded[ICPC_CONTROL_PAYLOAD_SIZE];
    size_t encoded_length = 0U;

    if (expect_status(icpc_control_encode(encoded, sizeof(encoded), &source,
                                          &encoded_length),
                      ICPC_CONTROL_STATUS_OK, "control encode") != 0) {
        return 1;
    }
    if (encoded_length != sizeof(encoded) ||
        memcmp(encoded, expected, sizeof(expected)) != 0) {
        fprintf(stderr, "control golden vector mismatch\n");
        return 1;
    }
    if (expect_status(icpc_control_decode(encoded, sizeof(encoded), &decoded),
                      ICPC_CONTROL_STATUS_OK, "control decode") != 0) {
        return 1;
    }
    if (memcmp(&source, &decoded, sizeof(source)) != 0) {
        fprintf(stderr, "control round trip mismatch\n");
        return 1;
    }
    return 0;
}

static int test_status_and_error_round_trip(void)
{
    const struct icpc_status_payload status_source = {
        .schema_version = ICPC_CONTROL_SCHEMA_VERSION,
        .control_mode = ICPC_CONTROL_MLP,
        .health_flags = ICPC_HEALTH_SAFE,
        .applied_request_id = 42U,
        .sample_index = 9U,
        .measured_mC = 30000,
        .target_mC = ICPC_CONTROL_TARGET_MILLI_CELSIUS,
        .duty_q16_16 = 32768,
        .control_error_mC = 25000,
    };
    const struct icpc_error_payload error_source = {
        .schema_version = ICPC_CONTROL_SCHEMA_VERSION,
        .subsystem = ICPC_SUBSYSTEM_NETWORK,
        .detail_code = 1U,
        .related_request_id = 42U,
        .diagnostic_token = UINT32_C(0x11223344),
    };
    struct icpc_status_payload decoded_status;
    struct icpc_error_payload decoded_error;
    uint8_t status_buffer[ICPC_STATUS_PAYLOAD_SIZE];
    uint8_t error_buffer[ICPC_ERROR_PAYLOAD_SIZE];
    size_t encoded_length = 0U;

    if (expect_status(icpc_status_encode(status_buffer, sizeof(status_buffer),
                                         &status_source, &encoded_length),
                      ICPC_CONTROL_STATUS_OK, "status encode") != 0 ||
        encoded_length != sizeof(status_buffer) ||
        expect_status(icpc_status_decode(status_buffer, sizeof(status_buffer),
                                         &decoded_status),
                      ICPC_CONTROL_STATUS_OK, "status decode") != 0 ||
        memcmp(&status_source, &decoded_status, sizeof(status_source)) != 0) {
        fprintf(stderr, "status round trip mismatch\n");
        return 1;
    }
    if (expect_status(icpc_error_encode(error_buffer, sizeof(error_buffer),
                                        &error_source, &encoded_length),
                      ICPC_CONTROL_STATUS_OK, "error encode") != 0 ||
        encoded_length != sizeof(error_buffer) ||
        expect_status(icpc_error_decode(error_buffer, sizeof(error_buffer),
                                        &decoded_error),
                      ICPC_CONTROL_STATUS_OK, "error decode") != 0 ||
        memcmp(&error_source, &decoded_error, sizeof(error_source)) != 0) {
        fprintf(stderr, "error round trip mismatch\n");
        return 1;
    }
    return 0;
}

static int test_rejections(void)
{
    struct icpc_control_payload control = {
        .schema_version = ICPC_CONTROL_SCHEMA_VERSION,
        .command = ICPC_CONTROL_APPLY_OUTPUT,
        .control_mode = ICPC_CONTROL_FIXED_BASELINE,
        .request_id = 1U,
        .duty_q16_16 = 0,
        .target_mC = ICPC_CONTROL_TARGET_MILLI_CELSIUS,
        .model_version = 0U,
        .validity_ms = ICPC_CONTROL_VALIDITY_MS,
    };
    struct icpc_status_payload status = {
        .schema_version = ICPC_CONTROL_SCHEMA_VERSION,
        .control_mode = ICPC_CONTROL_FIXED_BASELINE,
        .health_flags = 0U,
        .target_mC = ICPC_CONTROL_TARGET_MILLI_CELSIUS,
    };
    uint8_t buffer[ICPC_STATUS_PAYLOAD_SIZE];
    size_t encoded_length = 0U;

    control.request_id = 0U;
    if (expect_status(icpc_control_encode(buffer, sizeof(buffer), &control,
                                          &encoded_length),
                      ICPC_CONTROL_STATUS_INVALID_REQUEST,
                      "zero request id") != 0) {
        return 1;
    }
    control.request_id = 1U;
    control.validity_ms = 499U;
    if (expect_status(icpc_control_encode(buffer, sizeof(buffer), &control,
                                          &encoded_length),
                      ICPC_CONTROL_STATUS_INVALID_VALIDITY,
                      "invalid validity") != 0) {
        return 1;
    }
    control.validity_ms = ICPC_CONTROL_VALIDITY_MS;
    control.target_mC = 55001;
    if (expect_status(icpc_control_encode(buffer, sizeof(buffer), &control,
                                          &encoded_length),
                      ICPC_CONTROL_STATUS_INVALID_TARGET,
                      "invalid target") != 0) {
        return 1;
    }
    control.target_mC = ICPC_CONTROL_TARGET_MILLI_CELSIUS;
    buffer[0] = ICPC_CONTROL_SCHEMA_VERSION;
    buffer[1] = ICPC_CONTROL_APPLY_OUTPUT;
    buffer[2] = ICPC_CONTROL_FIXED_BASELINE;
    buffer[3] = 1U;
    if (expect_status(icpc_control_decode(buffer, ICPC_CONTROL_PAYLOAD_SIZE,
                                          &control),
                      ICPC_CONTROL_STATUS_INVALID_RESERVED,
                      "nonzero control reserved byte") != 0) {
        return 1;
    }
    status.health_flags = UINT16_C(0x8000);
    if (expect_status(icpc_status_encode(buffer, sizeof(buffer), &status,
                                         &encoded_length),
                      ICPC_CONTROL_STATUS_INVALID_HEALTH_FLAGS,
                      "unknown health flag") != 0) {
        return 1;
    }
    return expect_status(icpc_error_decode(buffer, 3U, NULL),
                         ICPC_CONTROL_STATUS_NULL_ARGUMENT,
                         "null error output");
}

int main(void)
{
    if (test_control_round_trip() != 0 ||
        test_status_and_error_round_trip() != 0 || test_rejections() != 0) {
        return 1;
    }
    puts("ICPC CONTROL_PAYLOAD_PASS");
    return 0;
}
