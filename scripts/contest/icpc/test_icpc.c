#include "icpc.h"

#include <stdio.h>
#include <string.h>

#define CHECK(condition)                                                        \
    do {                                                                        \
        if (!(condition)) {                                                     \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, \
                    #condition);                                                \
            return 1;                                                           \
        }                                                                       \
    } while (0)

static int test_crc32c_standard_vector(void);
static int test_control_round_trip(void);
static int test_ack_round_trip(void);
static int test_error_round_trip(void);
static int test_rejects_corruption_and_length_mismatch(void);
static int test_rejects_invalid_field_combinations(void);
static int test_sequence_comparison_wraps_without_ambiguity(void);
static int test_retry_state_uses_bounded_exponential_backoff(void);
static int test_retry_state_accepts_only_matching_ack(void);
static int test_receive_window_detects_duplicates_and_reordering(void);

int main(void)
{
    CHECK(test_crc32c_standard_vector() == 0);
    CHECK(test_control_round_trip() == 0);
    CHECK(test_ack_round_trip() == 0);
    CHECK(test_error_round_trip() == 0);
    CHECK(test_rejects_corruption_and_length_mismatch() == 0);
    CHECK(test_rejects_invalid_field_combinations() == 0);
    CHECK(test_sequence_comparison_wraps_without_ambiguity() == 0);
    CHECK(test_retry_state_uses_bounded_exponential_backoff() == 0);
    CHECK(test_retry_state_accepts_only_matching_ack() == 0);
    CHECK(test_receive_window_detects_duplicates_and_reordering() == 0);
    puts("ICPC_PROTOCOL_PASS");
    return 0;
}

static int test_crc32c_standard_vector(void)
{
    static const uint8_t input[] = "123456789";
    CHECK(icpc_crc32c(input, sizeof(input) - 1U) == UINT32_C(0xe3069283));
    return 0;
}

static int test_control_round_trip(void)
{
    static const uint8_t payload[] = {0x10U, 0x20U, 0x30U};
    uint8_t packet[ICPC_MAX_PACKET_SIZE];
    size_t packet_length = 0U;
    struct icpc_header input = {
        .message_type = ICPC_MESSAGE_CONTROL,
        .flags = ICPC_FLAG_ACK_REQUIRED,
        .session_id = UINT32_C(0x01020304),
        .sequence = UINT32_C(0x10203040),
        .ack_sequence = 0U,
        .timestamp_ms = UINT64_C(0x0102030405060708),
        .error_code = 0U,
    };
    struct icpc_header output;
    const uint8_t *decoded_payload = NULL;
    size_t decoded_length = 0U;

    CHECK(icpc_encode(packet, sizeof(packet), &input, payload, sizeof(payload),
                      &packet_length) == ICPC_STATUS_OK);
    CHECK(packet_length == ICPC_HEADER_SIZE + sizeof(payload));
    CHECK(packet[0] == 'I' && packet[1] == 'C' && packet[2] == 'P' &&
          packet[3] == 'C');
    CHECK(packet[8] == 0x01U && packet[9] == 0x02U && packet[10] == 0x03U &&
          packet[11] == 0x04U);
    CHECK(icpc_decode(packet, packet_length, &output, &decoded_payload,
                      &decoded_length) == ICPC_STATUS_OK);
    CHECK(output.message_type == input.message_type);
    CHECK(output.flags == input.flags);
    CHECK(output.session_id == input.session_id);
    CHECK(output.sequence == input.sequence);
    CHECK(output.ack_sequence == input.ack_sequence);
    CHECK(output.timestamp_ms == input.timestamp_ms);
    CHECK(output.error_code == input.error_code);
    CHECK(decoded_length == sizeof(payload));
    CHECK(memcmp(decoded_payload, payload, sizeof(payload)) == 0);
    return 0;
}

static int test_ack_round_trip(void)
{
    uint8_t packet[ICPC_HEADER_SIZE];
    size_t packet_length = 0U;
    struct icpc_header input = {
        .message_type = ICPC_MESSAGE_ACK,
        .flags = 0U,
        .session_id = 7U,
        .sequence = 9U,
        .ack_sequence = 8U,
        .timestamp_ms = 1234U,
        .error_code = 0U,
    };
    struct icpc_header output;
    const uint8_t *payload = NULL;
    size_t payload_length = 1U;

    CHECK(icpc_encode(packet, sizeof(packet), &input, NULL, 0U,
                      &packet_length) == ICPC_STATUS_OK);
    CHECK(icpc_decode(packet, packet_length, &output, &payload,
                      &payload_length) == ICPC_STATUS_OK);
    CHECK(output.ack_sequence == 8U);
    CHECK(payload_length == 0U);
    return 0;
}

static int test_error_round_trip(void)
{
    static const uint8_t payload[] = {'s', 't', 'a', 'l', 'e'};
    uint8_t packet[ICPC_MAX_PACKET_SIZE];
    size_t packet_length = 0U;
    struct icpc_header input = {
        .message_type = ICPC_MESSAGE_ERROR,
        .flags = ICPC_FLAG_ACK_REQUIRED,
        .session_id = 12U,
        .sequence = 99U,
        .ack_sequence = 0U,
        .timestamp_ms = 888U,
        .error_code = ICPC_ERROR_STALE_SEQUENCE,
    };
    struct icpc_header output;
    const uint8_t *decoded_payload = NULL;
    size_t decoded_length = 0U;

    CHECK(icpc_encode(packet, sizeof(packet), &input, payload, sizeof(payload),
                      &packet_length) == ICPC_STATUS_OK);
    CHECK(icpc_decode(packet, packet_length, &output, &decoded_payload,
                      &decoded_length) == ICPC_STATUS_OK);
    CHECK(output.error_code == ICPC_ERROR_STALE_SEQUENCE);
    CHECK(decoded_length == sizeof(payload));
    return 0;
}

static int test_rejects_corruption_and_length_mismatch(void)
{
    static const uint8_t payload[] = {1U, 2U};
    uint8_t packet[ICPC_MAX_PACKET_SIZE];
    size_t packet_length = 0U;
    struct icpc_header header = {
        .message_type = ICPC_MESSAGE_STATUS,
        .flags = 0U,
        .session_id = 1U,
        .sequence = 1U,
        .ack_sequence = 0U,
        .timestamp_ms = 0U,
        .error_code = 0U,
    };
    struct icpc_header output;
    const uint8_t *decoded_payload = NULL;
    size_t decoded_length = 0U;

    CHECK(icpc_encode(packet, sizeof(packet), &header, payload, sizeof(payload),
                      &packet_length) == ICPC_STATUS_OK);
    packet[ICPC_HEADER_SIZE] ^= 0x80U;
    CHECK(icpc_decode(packet, packet_length, &output, &decoded_payload,
                      &decoded_length) == ICPC_STATUS_CHECKSUM_MISMATCH);
    packet[ICPC_HEADER_SIZE] ^= 0x80U;
    CHECK(icpc_decode(packet, packet_length - 1U, &output, &decoded_payload,
                      &decoded_length) == ICPC_STATUS_LENGTH_MISMATCH);
    packet[packet_length] = 0U;
    CHECK(icpc_decode(packet, packet_length + 1U, &output, &decoded_payload,
                      &decoded_length) == ICPC_STATUS_LENGTH_MISMATCH);
    return 0;
}

static int test_rejects_invalid_field_combinations(void)
{
    uint8_t packet[ICPC_MAX_PACKET_SIZE];
    size_t packet_length = 0U;
    struct icpc_header header = {
        .message_type = ICPC_MESSAGE_CONTROL,
        .flags = ICPC_FLAG_RETRANSMISSION,
        .session_id = 1U,
        .sequence = 1U,
        .ack_sequence = 0U,
        .timestamp_ms = 0U,
        .error_code = 0U,
    };

    CHECK(icpc_encode(packet, sizeof(packet), &header, NULL, 0U,
                      &packet_length) == ICPC_STATUS_INVALID_FLAGS);
    header.message_type = ICPC_MESSAGE_ACK;
    header.flags = 0U;
    CHECK(icpc_encode(packet, sizeof(packet), &header, NULL, 0U,
                      &packet_length) == ICPC_STATUS_INVALID_ACK);
    header.message_type = ICPC_MESSAGE_ERROR;
    header.ack_sequence = 0U;
    CHECK(icpc_encode(packet, sizeof(packet), &header, NULL, 0U,
                      &packet_length) == ICPC_STATUS_INVALID_ERROR);
    header.message_type = ICPC_MESSAGE_HEARTBEAT;
    header.error_code = 0U;
    CHECK(icpc_encode(packet, sizeof(packet), &header, (const uint8_t *)"x", 1U,
                      &packet_length) == ICPC_STATUS_INVALID_PAYLOAD);
    return 0;
}

static int test_sequence_comparison_wraps_without_ambiguity(void)
{
    CHECK(icpc_compare_sequence(11U, 10U) == ICPC_SEQUENCE_NEWER);
    CHECK(icpc_compare_sequence(10U, 10U) == ICPC_SEQUENCE_EQUAL);
    CHECK(icpc_compare_sequence(10U, 11U) == ICPC_SEQUENCE_OLDER);
    CHECK(icpc_compare_sequence(0U, UINT32_MAX) == ICPC_SEQUENCE_NEWER);
    CHECK(icpc_compare_sequence(UINT32_C(0x80000000), 0U) ==
          ICPC_SEQUENCE_AMBIGUOUS);
    return 0;
}

static int test_retry_state_uses_bounded_exponential_backoff(void)
{
    struct icpc_retry_state state;

    icpc_retry_reset(&state);
    CHECK(icpc_retry_start(&state, 7U, 10U, 0U) == ICPC_STATUS_OK);
    CHECK(icpc_retry_poll(&state, 99U) == ICPC_RETRY_WAITING);
    CHECK(icpc_retry_poll(&state, 100U) == ICPC_RETRY_RETRANSMIT);
    CHECK(state.retransmissions == 1U && state.deadline_ms == 300U);
    CHECK(icpc_retry_poll(&state, 300U) == ICPC_RETRY_RETRANSMIT);
    CHECK(state.retransmissions == 2U && state.deadline_ms == 700U);
    CHECK(icpc_retry_poll(&state, 700U) == ICPC_RETRY_RETRANSMIT);
    CHECK(state.retransmissions == 3U && state.deadline_ms == 1100U);
    CHECK(icpc_retry_poll(&state, 1100U) == ICPC_RETRY_TIMED_OUT);
    CHECK(state.active == 0U);
    CHECK(icpc_retry_poll(&state, 1200U) == ICPC_RETRY_IDLE);
    return 0;
}

static int test_retry_state_accepts_only_matching_ack(void)
{
    struct icpc_retry_state state;
    struct icpc_header ack = {
        .message_type = ICPC_MESSAGE_ACK,
        .flags = 0U,
        .session_id = 9U,
        .sequence = 20U,
        .ack_sequence = 19U,
        .timestamp_ms = 0U,
        .error_code = 0U,
    };

    icpc_retry_reset(&state);
    CHECK(icpc_retry_accept_ack(&state, &ack) == ICPC_ACK_NOT_PENDING);
    CHECK(icpc_retry_start(&state, 9U, 18U, 0U) == ICPC_STATUS_OK);
    CHECK(icpc_retry_accept_ack(&state, &ack) == ICPC_ACK_MISMATCHED);
    CHECK(state.active == 1U);
    ack.ack_sequence = 18U;
    CHECK(icpc_retry_accept_ack(&state, &ack) == ICPC_ACK_MATCHED);
    CHECK(state.active == 0U);
    return 0;
}

static int test_receive_window_detects_duplicates_and_reordering(void)
{
    struct icpc_receive_window window;

    icpc_receive_window_reset(&window);
    CHECK(icpc_receive_window_observe(&window, 1U, 10U) ==
          ICPC_RECEIVE_NEWER);
    CHECK(icpc_receive_window_observe(&window, 1U, 10U) ==
          ICPC_RECEIVE_DUPLICATE);
    CHECK(icpc_receive_window_observe(&window, 1U, 12U) ==
          ICPC_RECEIVE_NEWER);
    CHECK(icpc_receive_window_observe(&window, 1U, 11U) ==
          ICPC_RECEIVE_OUT_OF_ORDER);
    CHECK(icpc_receive_window_observe(&window, 1U, 11U) ==
          ICPC_RECEIVE_DUPLICATE);
    CHECK(icpc_receive_window_observe(&window, 1U, UINT32_C(0xffffffcb)) ==
          ICPC_RECEIVE_STALE);
    CHECK(icpc_receive_window_observe(&window, 1U, UINT32_C(0x8000000c)) ==
          ICPC_RECEIVE_AMBIGUOUS);
    CHECK(icpc_receive_window_observe(&window, 2U, 1U) ==
          ICPC_RECEIVE_NEWER);
    CHECK(window.session_id == 2U && window.latest_sequence == 1U);
    return 0;
}
