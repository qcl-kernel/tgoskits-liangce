#include "icpc.h"

#define ICPC_CRC32C_POLYNOMIAL UINT32_C(0x82f63b78)
#define ICPC_KNOWN_FLAGS                                                      \
    (ICPC_FLAG_ACK_REQUIRED | ICPC_FLAG_RETRANSMISSION)

enum icpc_wire_offset {
    ICPC_OFFSET_MAGIC = 0,
    ICPC_OFFSET_VERSION = 4,
    ICPC_OFFSET_HEADER_LENGTH = 5,
    ICPC_OFFSET_MESSAGE_TYPE = 6,
    ICPC_OFFSET_FLAGS = 7,
    ICPC_OFFSET_SESSION_ID = 8,
    ICPC_OFFSET_SEQUENCE = 12,
    ICPC_OFFSET_ACK_SEQUENCE = 16,
    ICPC_OFFSET_TIMESTAMP_MS = 20,
    ICPC_OFFSET_PAYLOAD_LENGTH = 28,
    ICPC_OFFSET_ERROR_CODE = 30,
    ICPC_OFFSET_CRC32C = 32,
};

static enum icpc_status validate_header(const struct icpc_header *header,
                                        size_t payload_length);
static int is_known_message_type(uint8_t message_type);
static uint32_t crc32c_begin(void);
static uint32_t crc32c_update(uint32_t crc, const uint8_t *bytes,
                              size_t length);
static uint32_t crc32c_finish(uint32_t crc);
static uint32_t packet_crc32c(const uint8_t *packet, size_t packet_length);
static void write_u16_be(uint8_t *output, uint16_t value);
static void write_u32_be(uint8_t *output, uint32_t value);
static void write_u64_be(uint8_t *output, uint64_t value);
static uint16_t read_u16_be(const uint8_t *input);
static uint32_t read_u32_be(const uint8_t *input);
static uint64_t read_u64_be(const uint8_t *input);
static void copy_bytes(uint8_t *destination, const uint8_t *source,
                       size_t length);
static uint64_t add_milliseconds(uint64_t now_ms, uint32_t timeout_ms);

uint32_t icpc_crc32c(const uint8_t *bytes, size_t length)
{
    uint32_t crc;

    if (bytes == NULL && length != 0U) {
        return 0U;
    }

    crc = crc32c_begin();
    crc = crc32c_update(crc, bytes, length);
    return crc32c_finish(crc);
}

enum icpc_status icpc_encode(uint8_t *packet, size_t packet_capacity,
                             const struct icpc_header *header,
                             const uint8_t *payload, size_t payload_length,
                             size_t *packet_length)
{
    enum icpc_status status;
    size_t encoded_length;
    uint32_t checksum;

    if (packet == NULL || header == NULL || packet_length == NULL) {
        return ICPC_STATUS_NULL_ARGUMENT;
    }
    *packet_length = 0U;
    if (payload == NULL && payload_length != 0U) {
        return ICPC_STATUS_NULL_ARGUMENT;
    }
    if (payload_length > ICPC_MAX_PAYLOAD_SIZE) {
        return ICPC_STATUS_PAYLOAD_TOO_LARGE;
    }

    status = validate_header(header, payload_length);
    if (status != ICPC_STATUS_OK) {
        return status;
    }

    encoded_length = ICPC_HEADER_SIZE + payload_length;
    if (packet_capacity < encoded_length) {
        return ICPC_STATUS_BUFFER_TOO_SMALL;
    }

    packet[ICPC_OFFSET_MAGIC] = 'I';
    packet[ICPC_OFFSET_MAGIC + 1U] = 'C';
    packet[ICPC_OFFSET_MAGIC + 2U] = 'P';
    packet[ICPC_OFFSET_MAGIC + 3U] = 'C';
    packet[ICPC_OFFSET_VERSION] = ICPC_VERSION;
    packet[ICPC_OFFSET_HEADER_LENGTH] = ICPC_HEADER_SIZE;
    packet[ICPC_OFFSET_MESSAGE_TYPE] = header->message_type;
    packet[ICPC_OFFSET_FLAGS] = header->flags;
    write_u32_be(&packet[ICPC_OFFSET_SESSION_ID], header->session_id);
    write_u32_be(&packet[ICPC_OFFSET_SEQUENCE], header->sequence);
    write_u32_be(&packet[ICPC_OFFSET_ACK_SEQUENCE], header->ack_sequence);
    write_u64_be(&packet[ICPC_OFFSET_TIMESTAMP_MS], header->timestamp_ms);
    write_u16_be(&packet[ICPC_OFFSET_PAYLOAD_LENGTH],
                 (uint16_t)payload_length);
    write_u16_be(&packet[ICPC_OFFSET_ERROR_CODE], header->error_code);
    write_u32_be(&packet[ICPC_OFFSET_CRC32C], 0U);
    copy_bytes(&packet[ICPC_HEADER_SIZE], payload, payload_length);

    checksum = packet_crc32c(packet, encoded_length);
    write_u32_be(&packet[ICPC_OFFSET_CRC32C], checksum);
    *packet_length = encoded_length;
    return ICPC_STATUS_OK;
}

enum icpc_status icpc_decode(const uint8_t *packet, size_t packet_length,
                             struct icpc_header *header,
                             const uint8_t **payload, size_t *payload_length)
{
    enum icpc_status status;
    size_t declared_payload_length;
    uint32_t expected_checksum;
    uint32_t actual_checksum;

    if (packet == NULL || header == NULL || payload == NULL ||
        payload_length == NULL) {
        return ICPC_STATUS_NULL_ARGUMENT;
    }
    *payload = NULL;
    *payload_length = 0U;
    if (packet_length < ICPC_HEADER_SIZE) {
        return ICPC_STATUS_LENGTH_MISMATCH;
    }
    if (packet[ICPC_OFFSET_MAGIC] != 'I' ||
        packet[ICPC_OFFSET_MAGIC + 1U] != 'C' ||
        packet[ICPC_OFFSET_MAGIC + 2U] != 'P' ||
        packet[ICPC_OFFSET_MAGIC + 3U] != 'C') {
        return ICPC_STATUS_INVALID_MAGIC;
    }
    if (packet[ICPC_OFFSET_VERSION] != ICPC_VERSION) {
        return ICPC_STATUS_UNSUPPORTED_VERSION;
    }
    if (packet[ICPC_OFFSET_HEADER_LENGTH] != ICPC_HEADER_SIZE) {
        return ICPC_STATUS_INVALID_HEADER_LENGTH;
    }

    declared_payload_length = read_u16_be(&packet[ICPC_OFFSET_PAYLOAD_LENGTH]);
    if (declared_payload_length > ICPC_MAX_PAYLOAD_SIZE ||
        packet_length != ICPC_HEADER_SIZE + declared_payload_length) {
        return ICPC_STATUS_LENGTH_MISMATCH;
    }

    header->message_type = packet[ICPC_OFFSET_MESSAGE_TYPE];
    header->flags = packet[ICPC_OFFSET_FLAGS];
    header->session_id = read_u32_be(&packet[ICPC_OFFSET_SESSION_ID]);
    header->sequence = read_u32_be(&packet[ICPC_OFFSET_SEQUENCE]);
    header->ack_sequence = read_u32_be(&packet[ICPC_OFFSET_ACK_SEQUENCE]);
    header->timestamp_ms = read_u64_be(&packet[ICPC_OFFSET_TIMESTAMP_MS]);
    header->error_code = read_u16_be(&packet[ICPC_OFFSET_ERROR_CODE]);

    status = validate_header(header, declared_payload_length);
    if (status != ICPC_STATUS_OK) {
        return status;
    }

    expected_checksum = read_u32_be(&packet[ICPC_OFFSET_CRC32C]);
    actual_checksum = packet_crc32c(packet, packet_length);
    if (expected_checksum != actual_checksum) {
        return ICPC_STATUS_CHECKSUM_MISMATCH;
    }

    *payload = &packet[ICPC_HEADER_SIZE];
    *payload_length = declared_payload_length;
    return ICPC_STATUS_OK;
}

enum icpc_sequence_order icpc_compare_sequence(uint32_t candidate,
                                               uint32_t reference)
{
    uint32_t difference;

    if (candidate == reference) {
        return ICPC_SEQUENCE_EQUAL;
    }
    difference = candidate - reference;
    if (difference == UINT32_C(0x80000000)) {
        return ICPC_SEQUENCE_AMBIGUOUS;
    }
    if (difference < UINT32_C(0x80000000)) {
        return ICPC_SEQUENCE_NEWER;
    }
    return ICPC_SEQUENCE_OLDER;
}

void icpc_retry_reset(struct icpc_retry_state *state)
{
    if (state == NULL) {
        return;
    }
    state->deadline_ms = 0U;
    state->session_id = 0U;
    state->sequence = 0U;
    state->timeout_ms = 0U;
    state->retransmissions = 0U;
    state->active = 0U;
}

enum icpc_status icpc_retry_start(struct icpc_retry_state *state,
                                  uint32_t session_id, uint32_t sequence,
                                  uint64_t now_ms)
{
    if (state == NULL) {
        return ICPC_STATUS_NULL_ARGUMENT;
    }
    if (state->active != 0U) {
        return ICPC_STATUS_RETRY_ALREADY_ACTIVE;
    }
    if (session_id == 0U) {
        return ICPC_STATUS_INVALID_SESSION;
    }
    if (sequence == 0U) {
        return ICPC_STATUS_INVALID_SEQUENCE;
    }

    state->session_id = session_id;
    state->sequence = sequence;
    state->timeout_ms = ICPC_INITIAL_RETRY_TIMEOUT_MS;
    state->deadline_ms = add_milliseconds(now_ms, state->timeout_ms);
    state->retransmissions = 0U;
    state->active = 1U;
    return ICPC_STATUS_OK;
}

enum icpc_retry_action icpc_retry_poll(struct icpc_retry_state *state,
                                       uint64_t now_ms)
{
    uint32_t next_timeout;

    if (state == NULL) {
        return ICPC_RETRY_INVALID;
    }
    if (state->active == 0U) {
        return ICPC_RETRY_IDLE;
    }
    if (now_ms < state->deadline_ms) {
        return ICPC_RETRY_WAITING;
    }
    if (state->retransmissions >= ICPC_MAX_RETRANSMISSIONS) {
        state->active = 0U;
        return ICPC_RETRY_TIMED_OUT;
    }

    state->retransmissions++;
    next_timeout = state->timeout_ms * 2U;
    if (next_timeout > ICPC_MAX_RETRY_TIMEOUT_MS) {
        next_timeout = ICPC_MAX_RETRY_TIMEOUT_MS;
    }
    state->timeout_ms = next_timeout;
    state->deadline_ms = add_milliseconds(now_ms, next_timeout);
    return ICPC_RETRY_RETRANSMIT;
}

enum icpc_ack_result
icpc_retry_accept_ack(struct icpc_retry_state *state,
                      const struct icpc_header *acknowledgement)
{
    if (state == NULL || acknowledgement == NULL ||
        acknowledgement->message_type != ICPC_MESSAGE_ACK) {
        return ICPC_ACK_INVALID;
    }
    if (state->active == 0U) {
        return ICPC_ACK_NOT_PENDING;
    }
    if (acknowledgement->session_id != state->session_id ||
        acknowledgement->ack_sequence != state->sequence) {
        return ICPC_ACK_MISMATCHED;
    }

    state->active = 0U;
    return ICPC_ACK_MATCHED;
}

void icpc_receive_window_reset(struct icpc_receive_window *window)
{
    if (window == NULL) {
        return;
    }
    window->received_bitmap = 0U;
    window->session_id = 0U;
    window->latest_sequence = 0U;
    window->initialized = 0U;
}

enum icpc_receive_result
icpc_receive_window_observe(struct icpc_receive_window *window,
                            uint32_t session_id, uint32_t sequence)
{
    enum icpc_sequence_order order;
    uint32_t distance;
    uint64_t sequence_bit;

    if (window == NULL || session_id == 0U || sequence == 0U) {
        return ICPC_RECEIVE_INVALID;
    }
    if (window->initialized == 0U || window->session_id != session_id) {
        window->received_bitmap = 1U;
        window->session_id = session_id;
        window->latest_sequence = sequence;
        window->initialized = 1U;
        return ICPC_RECEIVE_NEWER;
    }

    order = icpc_compare_sequence(sequence, window->latest_sequence);
    if (order == ICPC_SEQUENCE_EQUAL) {
        return ICPC_RECEIVE_DUPLICATE;
    }
    if (order == ICPC_SEQUENCE_AMBIGUOUS) {
        return ICPC_RECEIVE_AMBIGUOUS;
    }
    if (order == ICPC_SEQUENCE_NEWER) {
        distance = sequence - window->latest_sequence;
        if (distance >= ICPC_RECEIVE_WINDOW_BITS) {
            window->received_bitmap = 1U;
        } else {
            window->received_bitmap =
                (window->received_bitmap << distance) | UINT64_C(1);
        }
        window->latest_sequence = sequence;
        return ICPC_RECEIVE_NEWER;
    }

    distance = window->latest_sequence - sequence;
    if (distance >= ICPC_RECEIVE_WINDOW_BITS) {
        return ICPC_RECEIVE_STALE;
    }
    sequence_bit = UINT64_C(1) << distance;
    if ((window->received_bitmap & sequence_bit) != 0U) {
        return ICPC_RECEIVE_DUPLICATE;
    }
    window->received_bitmap |= sequence_bit;
    return ICPC_RECEIVE_OUT_OF_ORDER;
}

static enum icpc_status validate_header(const struct icpc_header *header,
                                        size_t payload_length)
{
    if (!is_known_message_type(header->message_type)) {
        return ICPC_STATUS_UNKNOWN_MESSAGE_TYPE;
    }
    if ((header->flags & (uint8_t)~ICPC_KNOWN_FLAGS) != 0U ||
        ((header->flags & ICPC_FLAG_RETRANSMISSION) != 0U &&
         (header->flags & ICPC_FLAG_ACK_REQUIRED) == 0U)) {
        return ICPC_STATUS_INVALID_FLAGS;
    }
    if (header->session_id == 0U) {
        return ICPC_STATUS_INVALID_SESSION;
    }
    if (header->sequence == 0U) {
        return ICPC_STATUS_INVALID_SEQUENCE;
    }
    if (header->message_type == ICPC_MESSAGE_ACK) {
        if (header->ack_sequence == 0U || header->flags != 0U ||
            payload_length != 0U) {
            return ICPC_STATUS_INVALID_ACK;
        }
    } else if (header->ack_sequence != 0U) {
        return ICPC_STATUS_INVALID_ACK;
    }
    if (header->message_type == ICPC_MESSAGE_ERROR) {
        if (header->error_code == ICPC_ERROR_NONE) {
            return ICPC_STATUS_INVALID_ERROR;
        }
    } else if (header->error_code != ICPC_ERROR_NONE) {
        return ICPC_STATUS_INVALID_ERROR;
    }
    if (header->message_type == ICPC_MESSAGE_HEARTBEAT &&
        payload_length != 0U) {
        return ICPC_STATUS_INVALID_PAYLOAD;
    }
    return ICPC_STATUS_OK;
}

static int is_known_message_type(uint8_t message_type)
{
    return message_type >= ICPC_MESSAGE_CONTROL &&
           message_type <= ICPC_MESSAGE_HEARTBEAT;
}

static uint32_t crc32c_begin(void)
{
    return UINT32_MAX;
}

static uint32_t crc32c_update(uint32_t crc, const uint8_t *bytes,
                              size_t length)
{
    size_t byte_index;

    for (byte_index = 0U; byte_index < length; ++byte_index) {
        unsigned int bit_index;

        crc ^= bytes[byte_index];
        for (bit_index = 0U; bit_index < 8U; ++bit_index) {
            uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1U) ^ (ICPC_CRC32C_POLYNOMIAL & mask);
        }
    }
    return crc;
}

static uint32_t crc32c_finish(uint32_t crc)
{
    return ~crc;
}

static uint32_t packet_crc32c(const uint8_t *packet, size_t packet_length)
{
    static const uint8_t zeros[4] = {0U, 0U, 0U, 0U};
    uint32_t crc = crc32c_begin();

    crc = crc32c_update(crc, packet, ICPC_OFFSET_CRC32C);
    crc = crc32c_update(crc, zeros, sizeof(zeros));
    crc = crc32c_update(crc, &packet[ICPC_HEADER_SIZE],
                        packet_length - ICPC_HEADER_SIZE);
    return crc32c_finish(crc);
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

static void copy_bytes(uint8_t *destination, const uint8_t *source,
                       size_t length)
{
    size_t index;

    for (index = 0U; index < length; ++index) {
        destination[index] = source[index];
    }
}

static uint64_t add_milliseconds(uint64_t now_ms, uint32_t timeout_ms)
{
    if (UINT64_MAX - now_ms < timeout_ms) {
        return UINT64_MAX;
    }
    return now_ms + timeout_ms;
}
