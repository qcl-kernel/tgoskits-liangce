#ifndef ICPC_H
#define ICPC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ICPC_VERSION 1U
#define ICPC_HEADER_SIZE 36U
#define ICPC_MAX_PAYLOAD_SIZE 1024U
#define ICPC_MAX_PACKET_SIZE (ICPC_HEADER_SIZE + ICPC_MAX_PAYLOAD_SIZE)

#define ICPC_FLAG_ACK_REQUIRED 0x01U
#define ICPC_FLAG_RETRANSMISSION 0x02U

#define ICPC_INITIAL_RETRY_TIMEOUT_MS 100U
#define ICPC_MAX_RETRY_TIMEOUT_MS 400U
#define ICPC_MAX_RETRANSMISSIONS 3U
#define ICPC_RECEIVE_WINDOW_BITS 64U

enum icpc_message_type {
    ICPC_MESSAGE_CONTROL = 1,
    ICPC_MESSAGE_STATUS = 2,
    ICPC_MESSAGE_ERROR = 3,
    ICPC_MESSAGE_ACK = 4,
    ICPC_MESSAGE_HEARTBEAT = 5,
};

enum icpc_error_code {
    ICPC_ERROR_NONE = 0,
    ICPC_ERROR_UNSUPPORTED_COMMAND = 1,
    ICPC_ERROR_INVALID_PAYLOAD = 2,
    ICPC_ERROR_STALE_SEQUENCE = 3,
    ICPC_ERROR_CONTROL_FAILURE = 4,
};

enum icpc_status {
    ICPC_STATUS_OK = 0,
    ICPC_STATUS_NULL_ARGUMENT,
    ICPC_STATUS_BUFFER_TOO_SMALL,
    ICPC_STATUS_PAYLOAD_TOO_LARGE,
    ICPC_STATUS_INVALID_MAGIC,
    ICPC_STATUS_UNSUPPORTED_VERSION,
    ICPC_STATUS_INVALID_HEADER_LENGTH,
    ICPC_STATUS_UNKNOWN_MESSAGE_TYPE,
    ICPC_STATUS_INVALID_FLAGS,
    ICPC_STATUS_INVALID_SESSION,
    ICPC_STATUS_INVALID_SEQUENCE,
    ICPC_STATUS_INVALID_ACK,
    ICPC_STATUS_INVALID_ERROR,
    ICPC_STATUS_INVALID_PAYLOAD,
    ICPC_STATUS_LENGTH_MISMATCH,
    ICPC_STATUS_CHECKSUM_MISMATCH,
    ICPC_STATUS_RETRY_ALREADY_ACTIVE,
};

enum icpc_sequence_order {
    ICPC_SEQUENCE_OLDER = -1,
    ICPC_SEQUENCE_EQUAL = 0,
    ICPC_SEQUENCE_NEWER = 1,
    ICPC_SEQUENCE_AMBIGUOUS = 2,
};

enum icpc_retry_action {
    ICPC_RETRY_INVALID = -1,
    ICPC_RETRY_IDLE = 0,
    ICPC_RETRY_WAITING,
    ICPC_RETRY_RETRANSMIT,
    ICPC_RETRY_TIMED_OUT,
};

enum icpc_ack_result {
    ICPC_ACK_INVALID = -1,
    ICPC_ACK_NOT_PENDING = 0,
    ICPC_ACK_MISMATCHED,
    ICPC_ACK_MATCHED,
};

enum icpc_receive_result {
    ICPC_RECEIVE_INVALID = -1,
    ICPC_RECEIVE_NEWER = 0,
    ICPC_RECEIVE_DUPLICATE,
    ICPC_RECEIVE_OUT_OF_ORDER,
    ICPC_RECEIVE_STALE,
    ICPC_RECEIVE_AMBIGUOUS,
};

struct icpc_header {
    uint8_t message_type;
    uint8_t flags;
    uint32_t session_id;
    uint32_t sequence;
    uint32_t ack_sequence;
    uint64_t timestamp_ms;
    uint16_t error_code;
};

struct icpc_retry_state {
    uint64_t deadline_ms;
    uint32_t session_id;
    uint32_t sequence;
    uint32_t timeout_ms;
    uint8_t retransmissions;
    uint8_t active;
};

struct icpc_receive_window {
    uint64_t received_bitmap;
    uint32_t session_id;
    uint32_t latest_sequence;
    uint8_t initialized;
};

uint32_t icpc_crc32c(const uint8_t *bytes, size_t length);

enum icpc_status icpc_encode(uint8_t *packet, size_t packet_capacity,
                             const struct icpc_header *header,
                             const uint8_t *payload, size_t payload_length,
                             size_t *packet_length);

enum icpc_status icpc_decode(const uint8_t *packet, size_t packet_length,
                             struct icpc_header *header,
                             const uint8_t **payload, size_t *payload_length);

enum icpc_sequence_order icpc_compare_sequence(uint32_t candidate,
                                               uint32_t reference);

void icpc_retry_reset(struct icpc_retry_state *state);

enum icpc_status icpc_retry_start(struct icpc_retry_state *state,
                                  uint32_t session_id, uint32_t sequence,
                                  uint64_t now_ms);

enum icpc_retry_action icpc_retry_poll(struct icpc_retry_state *state,
                                       uint64_t now_ms);

enum icpc_ack_result
icpc_retry_accept_ack(struct icpc_retry_state *state,
                      const struct icpc_header *acknowledgement);

void icpc_receive_window_reset(struct icpc_receive_window *window);

enum icpc_receive_result
icpc_receive_window_observe(struct icpc_receive_window *window,
                            uint32_t session_id, uint32_t sequence);

#ifdef __cplusplus
}
#endif

#endif
