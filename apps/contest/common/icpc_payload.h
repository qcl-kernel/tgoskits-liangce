#ifndef CONTEST_ICPC_PAYLOAD_H
#define CONTEST_ICPC_PAYLOAD_H

/* Freestanding big-endian codec for the frozen ICPC v1 app payloads. */

#include <stddef.h>
#include <stdint.h>

#define CONTEST_ICPC_CONTROL_PAYLOAD_SIZE 24U
#define CONTEST_ICPC_STATUS_PAYLOAD_SIZE 32U

static inline uint16_t contest_icpc_read_u16_be(const uint8_t *input)
{
    return (uint16_t)(((uint16_t)input[0] << 8U) | input[1]);
}

static inline uint32_t contest_icpc_read_u32_be(const uint8_t *input)
{
    return ((uint32_t)input[0] << 24U) |
           ((uint32_t)input[1] << 16U) |
           ((uint32_t)input[2] << 8U) |
           (uint32_t)input[3];
}

static inline uint64_t contest_icpc_read_u64_be(const uint8_t *input)
{
    uint64_t value = 0U;
    size_t index;
    for (index = 0U; index < 8U; ++index) {
        value = (value << 8U) | input[index];
    }
    return value;
}

static inline int32_t contest_icpc_read_i32_be(const uint8_t *input)
{
    return (int32_t)contest_icpc_read_u32_be(input);
}

static inline void contest_icpc_write_u16_be(uint8_t *output, uint16_t value)
{
    output[0] = (uint8_t)(value >> 8U);
    output[1] = (uint8_t)value;
}

static inline void contest_icpc_write_u32_be(uint8_t *output, uint32_t value)
{
    output[0] = (uint8_t)(value >> 24U);
    output[1] = (uint8_t)(value >> 16U);
    output[2] = (uint8_t)(value >> 8U);
    output[3] = (uint8_t)value;
}

static inline void contest_icpc_write_u64_be(uint8_t *output, uint64_t value)
{
    size_t index;
    for (index = 0U; index < 8U; ++index) {
        output[7U - index] = (uint8_t)value;
        value >>= 8U;
    }
}

static inline void contest_icpc_write_i32_be(uint8_t *output, int32_t value)
{
    contest_icpc_write_u32_be(output, (uint32_t)value);
}

#endif /* CONTEST_ICPC_PAYLOAD_H */
