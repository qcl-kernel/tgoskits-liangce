#include "runtime_config.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define PRESENT_BIND UINT32_C(1)
#define PRESENT_PEER UINT32_C(2)
#define PRESENT_UDP UINT32_C(4)
#define PRESENT_TCP UINT32_C(8)
#define PRESENT_MODEL UINT32_C(16)
#define PRESENT_METADATA UINT32_C(32)
#define PRESENT_RUN_ID UINT32_C(64)
#define PRESENT_EVENT_LOG UINT32_C(128)
#define PRESENT_SEED UINT32_C(256)
#define PRESENT_TICKS UINT32_C(512)
#define PRESENT_SESSION_ID UINT32_C(1024)

#define FROZEN_BIND_IP "10.77.0.1"
#define FROZEN_PEER_IP "10.77.0.2"
#define FROZEN_UDP_PORT UINT16_C(46000)
#define FROZEN_TCP_PORT UINT16_C(46001)
#define FROZEN_MODEL_PATH "/opt/tgos/model.bin"
#define FROZEN_METADATA_PATH "/opt/tgos/metadata.json"
#define FROZEN_EVENT_LOG "/run/tgos/linux-events.jsonl"
#define FROZEN_MODEL_VERSION UINT32_C(731924617)
#define FROZEN_MODEL_SHA256 \
    "2ba04889fddb129be4fb42c76c595513896ba15b3aee3b65b4d6ff3dca46d50f"
#define FROZEN_MODEL_SIZE 164U

static void set_error(char *error, size_t capacity, const char *message)
{
    if (error == NULL || capacity == 0U) {
        return;
    }
    (void)snprintf(error, capacity, "%s", message);
}

static int copy_text(char *destination, size_t capacity, const char *value,
                     char *error, size_t error_capacity)
{
    size_t length;

    if (value == NULL || value[0] == '\0') {
        set_error(error, error_capacity, "option value must be non-empty");
        return -1;
    }
    length = strlen(value);
    if (length >= capacity) {
        set_error(error, error_capacity, "option value is too long");
        return -1;
    }
    memcpy(destination, value, length + 1U);
    return 0;
}

/* Return 1 for a match, 0 for no match, and -1 for a missing value. */
static int option_value(int argc, char **argv, int *index, const char *arg,
                        const char *name, const char **value)
{
    size_t name_length = strlen(name);

    if (strcmp(arg, name) == 0) {
        if (*index + 1 >= argc || argv[*index + 1][0] == '\0') {
            return -1;
        }
        *index += 1;
        *value = argv[*index];
        return 1;
    }
    if (strncmp(arg, name, name_length) == 0 && arg[name_length] == '=') {
        if (arg[name_length + 1U] == '\0') {
            return -1;
        }
        *value = &arg[name_length + 1U];
        return 1;
    }
    return 0;
}

static int parse_u32(const char *value, uint32_t *output, uint32_t minimum,
                     uint32_t maximum, char *error, size_t error_capacity)
{
    unsigned long parsed;
    char *end = NULL;

    errno = 0;
    parsed = strtoul(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' ||
        parsed < (unsigned long)minimum || parsed > (unsigned long)maximum) {
        set_error(error, error_capacity, "numeric option is outside its range");
        return -1;
    }
    *output = (uint32_t)parsed;
    return 0;
}

static int mode_from_text(const char *value, enum contest_runtime_mode *mode)
{
    if (strcmp(value, "l3-smoke") == 0) {
        *mode = CONTEST_RUNTIME_L3_SMOKE;
    } else if (strcmp(value, "udp-echo") == 0) {
        *mode = CONTEST_RUNTIME_UDP_ECHO;
    } else if (strcmp(value, "tcp-client") == 0) {
        *mode = CONTEST_RUNTIME_TCP_CLIENT;
    } else if (strcmp(value, "udp-reliability") == 0) {
        *mode = CONTEST_RUNTIME_UDP_RELIABILITY;
    } else if (strcmp(value, "tcp-reliability") == 0) {
        *mode = CONTEST_RUNTIME_TCP_RELIABILITY;
    } else if (strcmp(value, "icpc-reliability") == 0) {
        *mode = CONTEST_RUNTIME_ICPC_RELIABILITY;
    } else if (strcmp(value, "icpc") == 0) {
        *mode = CONTEST_RUNTIME_ICPC;
    } else if (strcmp(value, "icpc-safe") == 0) {
        *mode = CONTEST_RUNTIME_ICPC_SAFE;
    } else if (strcmp(value, "ai-verify") == 0) {
        *mode = CONTEST_RUNTIME_AI_VERIFY;
    } else if (strcmp(value, "fixed") == 0) {
        *mode = CONTEST_RUNTIME_FIXED;
    } else if (strcmp(value, "mlp") == 0) {
        *mode = CONTEST_RUNTIME_MLP;
    } else {
        return -1;
    }
    return 0;
}

static int is_ai_mode(enum contest_runtime_mode mode)
{
    return mode == CONTEST_RUNTIME_FIXED || mode == CONTEST_RUNTIME_MLP;
}

static int is_safe_run_id(const char *run_id)
{
    size_t index;

    if (run_id[0] == '\0') {
        return 0;
    }
    for (index = 0U; run_id[index] != '\0'; ++index) {
        char c = run_id[index];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.')) {
            return 0;
        }
    }
    return strstr(run_id, "..") == NULL;
}

int contest_parse_runtime_config(int argc, char **argv,
                                 struct contest_runtime_config *config,
                                 char *error, size_t error_capacity)
{
    int index;
    const char *value;
    uint32_t parsed;

    if (config == NULL || argc < 1 || argv == NULL) {
        set_error(error, error_capacity, "invalid CLI arguments");
        return -1;
    }
    memset(config, 0, sizeof(*config));
    config->mode = CONTEST_RUNTIME_L3_SMOKE;
    (void)snprintf(config->mode_text, sizeof(config->mode_text), "%s",
                   "l3-smoke");
    (void)snprintf(config->bind_ip, sizeof(config->bind_ip), "%s",
                   FROZEN_BIND_IP);
    (void)snprintf(config->peer_ip, sizeof(config->peer_ip), "%s",
                   FROZEN_PEER_IP);
    config->udp_port = FROZEN_UDP_PORT;
    config->tcp_port = FROZEN_TCP_PORT;
    config->ticks = 100;

    for (index = 1; index < argc; ++index) {
        int matched;
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--mode",
                               &value);
        if (matched != 0) {
            if (matched < 0 || mode_from_text(value, &config->mode) != 0 ||
                copy_text(config->mode_text, sizeof(config->mode_text), value,
                          error, error_capacity) != 0) {
                set_error(error, error_capacity, "invalid --mode");
                return -1;
            }
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--bind",
                               &value);
        if (matched == 0) {
            matched = option_value(argc, argv, &index, argv[index],
                                   "--bind-ip", &value);
        }
        if (matched != 0) {
            if (matched < 0 || copy_text(config->bind_ip, sizeof(config->bind_ip),
                                         value, error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_BIND;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--peer",
                               &value);
        if (matched == 0) {
            matched = option_value(argc, argv, &index, argv[index],
                                   "--peer-ip", &value);
        }
        if (matched != 0) {
            if (matched < 0 || copy_text(config->peer_ip, sizeof(config->peer_ip),
                                         value, error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_PEER;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--udp-port",
                               &value);
        if (matched != 0) {
            if (matched < 0 || parse_u32(value, &parsed, 1U, 65535U, error,
                                         error_capacity) != 0) {
                return -1;
            }
            config->udp_port = (uint16_t)parsed;
            config->present |= PRESENT_UDP;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--tcp-port",
                               &value);
        if (matched != 0) {
            if (matched < 0 || parse_u32(value, &parsed, 1U, 65535U, error,
                                         error_capacity) != 0) {
                return -1;
            }
            config->tcp_port = (uint16_t)parsed;
            config->present |= PRESENT_TCP;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--model",
                               &value);
        if (matched != 0) {
            if (matched < 0 || copy_text(config->model_path,
                                         sizeof(config->model_path), value,
                                         error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_MODEL;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--metadata",
                               &value);
        if (matched != 0) {
            if (matched < 0 || copy_text(config->metadata_path,
                                         sizeof(config->metadata_path), value,
                                         error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_METADATA;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--run-id",
                               &value);
        if (matched != 0) {
            if (matched < 0 || copy_text(config->run_id, sizeof(config->run_id),
                                         value, error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_RUN_ID;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--event-log",
                               &value);
        if (matched != 0) {
            if (matched < 0 || copy_text(config->event_log,
                                         sizeof(config->event_log), value,
                                         error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_EVENT_LOG;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--session-id",
                               &value);
        if (matched != 0) {
            if (matched < 0 || parse_u32(value, &config->session_id, 1U,
                                         UINT32_MAX, error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_SESSION_ID;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--seed",
                               &value);
        if (matched != 0) {
            if (matched < 0 || parse_u32(value, &config->seed, 1U,
                                         UINT32_MAX, error, error_capacity) != 0) {
                return -1;
            }
            config->present |= PRESENT_SEED;
            continue;
        }
        value = NULL;
        matched = option_value(argc, argv, &index, argv[index], "--ticks",
                               &value);
        if (matched != 0) {
            if (matched < 0 || parse_u32(value, &parsed, 1U, 1800U, error,
                                         error_capacity) != 0 ||
                (parsed != 100U && parsed != 1800U)) {
                set_error(error, error_capacity, "--ticks must be 100 or 1800");
                return -1;
            }
            config->ticks = (int)parsed;
            config->present |= PRESENT_TICKS;
            continue;
        }
        set_error(error, error_capacity, "unknown or unsupported CLI option");
        return -1;
    }
    /* The documented fixed/mlp CLI deliberately omits a second tick knob;
     * those modes are always the 1800-tick qualification trajectory. */
    if (is_ai_mode(config->mode) &&
        (config->present & PRESENT_TICKS) == 0U) {
        config->ticks = 1800;
    }
    return contest_validate_runtime_config(config, error, error_capacity);
}

int contest_validate_runtime_config(const struct contest_runtime_config *config,
                                    char *error, size_t error_capacity)
{
    unsigned required;

    if (config == NULL) {
        set_error(error, error_capacity, "runtime config is null");
        return -1;
    }
    if (config->udp_port != FROZEN_UDP_PORT ||
        config->tcp_port != FROZEN_TCP_PORT ||
        strcmp(config->bind_ip, FROZEN_BIND_IP) != 0 ||
        strcmp(config->peer_ip, FROZEN_PEER_IP) != 0) {
        set_error(error, error_capacity, "network CLI values drift from frozen contract");
        return -1;
    }
    if (!is_ai_mode(config->mode)) {
        return 0;
    }
    required = PRESENT_BIND | PRESENT_PEER | PRESENT_UDP | PRESENT_TCP |
               PRESENT_MODEL | PRESENT_METADATA | PRESENT_RUN_ID |
               PRESENT_EVENT_LOG | PRESENT_SESSION_ID | PRESENT_SEED;
    if ((config->present & required) != required) {
        set_error(error, error_capacity,
                  "fixed/mlp requires the complete documented CLI");
        return -1;
    }
    if (config->ticks != 1800) {
        set_error(error, error_capacity, "fixed/mlp requires 1800 ticks");
        return -1;
    }
    if (config->seed != 7U && config->seed != 19U && config->seed != 43U) {
        set_error(error, error_capacity, "seed must be 7, 19, or 43");
        return -1;
    }
    if (strcmp(config->model_path, FROZEN_MODEL_PATH) != 0 ||
        strcmp(config->metadata_path, FROZEN_METADATA_PATH) != 0 ||
        strcmp(config->event_log, FROZEN_EVENT_LOG) != 0 ||
        !is_safe_run_id(config->run_id)) {
        set_error(error, error_capacity,
                  "model, metadata, event-log, or run-id is not canonical");
        return -1;
    }
    return 0;
}

struct sha256_context {
    uint32_t state[8];
    uint64_t total;
    uint8_t buffer[64];
    size_t used;
};

static uint32_t rotr32(uint32_t value, unsigned count)
{
    return (value >> count) | (value << (32U - count));
}

static void sha256_transform(struct sha256_context *context,
                             const uint8_t block[64])
{
    static const uint32_t k[64] = {
        UINT32_C(0x428a2f98), UINT32_C(0x71374491), UINT32_C(0xb5c0fbcf),
        UINT32_C(0xe9b5dba5), UINT32_C(0x3956c25b), UINT32_C(0x59f111f1),
        UINT32_C(0x923f82a4), UINT32_C(0xab1c5ed5), UINT32_C(0xd807aa98),
        UINT32_C(0x12835b01), UINT32_C(0x243185be), UINT32_C(0x550c7dc3),
        UINT32_C(0x72be5d74), UINT32_C(0x80deb1fe), UINT32_C(0x9bdc06a7),
        UINT32_C(0xc19bf174), UINT32_C(0xe49b69c1), UINT32_C(0xefbe4786),
        UINT32_C(0x0fc19dc6), UINT32_C(0x240ca1cc), UINT32_C(0x2de92c6f),
        UINT32_C(0x4a7484aa), UINT32_C(0x5cb0a9dc), UINT32_C(0x76f988da),
        UINT32_C(0x983e5152), UINT32_C(0xa831c66d), UINT32_C(0xb00327c8),
        UINT32_C(0xbf597fc7), UINT32_C(0xc6e00bf3), UINT32_C(0xd5a79147),
        UINT32_C(0x06ca6351), UINT32_C(0x14292967), UINT32_C(0x27b70a85),
        UINT32_C(0x2e1b2138), UINT32_C(0x4d2c6dfc), UINT32_C(0x53380d13),
        UINT32_C(0x650a7354), UINT32_C(0x766a0abb), UINT32_C(0x81c2c92e),
        UINT32_C(0x92722c85), UINT32_C(0xa2bfe8a1), UINT32_C(0xa81a664b),
        UINT32_C(0xc24b8b70), UINT32_C(0xc76c51a3), UINT32_C(0xd192e819),
        UINT32_C(0xd6990624), UINT32_C(0xf40e3585), UINT32_C(0x106aa070),
        UINT32_C(0x19a4c116), UINT32_C(0x1e376c08), UINT32_C(0x2748774c),
        UINT32_C(0x34b0bcb5), UINT32_C(0x391c0cb3), UINT32_C(0x4ed8aa4a),
        UINT32_C(0x5b9cca4f), UINT32_C(0x682e6ff3), UINT32_C(0x748f82ee),
        UINT32_C(0x78a5636f), UINT32_C(0x84c87814), UINT32_C(0x8cc70208),
        UINT32_C(0x90befffa), UINT32_C(0xa4506ceb), UINT32_C(0xbef9a3f7),
        UINT32_C(0xc67178f2),
    };
    uint32_t words[64];
    uint32_t a, b, c, d, e, f, g, h;
    unsigned i;

    for (i = 0U; i < 16U; ++i) {
        words[i] = ((uint32_t)block[i * 4U] << 24U) |
                   ((uint32_t)block[i * 4U + 1U] << 16U) |
                   ((uint32_t)block[i * 4U + 2U] << 8U) |
                   (uint32_t)block[i * 4U + 3U];
    }
    for (i = 16U; i < 64U; ++i) {
        uint32_t s0 = rotr32(words[i - 15U], 7U) ^
                      rotr32(words[i - 15U], 18U) ^ (words[i - 15U] >> 3U);
        uint32_t s1 = rotr32(words[i - 2U], 17U) ^
                      rotr32(words[i - 2U], 19U) ^ (words[i - 2U] >> 10U);
        words[i] = words[i - 16U] + s0 + words[i - 7U] + s1;
    }
    a = context->state[0]; b = context->state[1]; c = context->state[2];
    d = context->state[3]; e = context->state[4]; f = context->state[5];
    g = context->state[6]; h = context->state[7];
    for (i = 0U; i < 64U; ++i) {
        uint32_t s1 = rotr32(e, 6U) ^ rotr32(e, 11U) ^ rotr32(e, 25U);
        uint32_t choose = (e & f) ^ ((~e) & g);
        uint32_t temp1 = h + s1 + choose + k[i] + words[i];
        uint32_t s0 = rotr32(a, 2U) ^ rotr32(a, 13U) ^ rotr32(a, 22U);
        uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = s0 + majority;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    context->state[0] += a; context->state[1] += b; context->state[2] += c;
    context->state[3] += d; context->state[4] += e; context->state[5] += f;
    context->state[6] += g; context->state[7] += h;
}

static void sha256_init(struct sha256_context *context)
{
    static const uint32_t initial[8] = {
        UINT32_C(0x6a09e667), UINT32_C(0xbb67ae85), UINT32_C(0x3c6ef372),
        UINT32_C(0xa54ff53a), UINT32_C(0x510e527f), UINT32_C(0x9b05688c),
        UINT32_C(0x1f83d9ab), UINT32_C(0x5be0cd19),
    };
    memcpy(context->state, initial, sizeof(initial));
    context->total = 0U;
    context->used = 0U;
}

static void sha256_update(struct sha256_context *context, const uint8_t *data,
                          size_t length)
{
    while (length > 0U) {
        size_t available = sizeof(context->buffer) - context->used;
        size_t take = length < available ? length : available;
        memcpy(&context->buffer[context->used], data, take);
        context->used += take;
        context->total += (uint64_t)take;
        data += take;
        length -= take;
        if (context->used == sizeof(context->buffer)) {
            sha256_transform(context, context->buffer);
            context->used = 0U;
        }
    }
}

static void sha256_final(struct sha256_context *context, uint8_t output[32])
{
    uint64_t bit_length = context->total * UINT64_C(8);
    unsigned i;

    context->buffer[context->used++] = UINT8_C(0x80);
    while (context->used != 56U) {
        if (context->used == sizeof(context->buffer)) {
            sha256_transform(context, context->buffer);
            context->used = 0U;
        }
        context->buffer[context->used++] = 0U;
    }
    for (i = 0U; i < 8U; ++i) {
        context->buffer[56U + i] = (uint8_t)(bit_length >> (56U - i * 8U));
    }
    sha256_transform(context, context->buffer);
    for (i = 0U; i < 8U; ++i) {
        output[i * 4U] = (uint8_t)(context->state[i] >> 24U);
        output[i * 4U + 1U] = (uint8_t)(context->state[i] >> 16U);
        output[i * 4U + 2U] = (uint8_t)(context->state[i] >> 8U);
        output[i * 4U + 3U] = (uint8_t)context->state[i];
    }
}

static int sha256_file(const char *path, char output[65])
{
    struct sha256_context context;
    uint8_t input[4096];
    uint8_t digest[32];
    FILE *file;
    size_t count;
    unsigned i;

    file = fopen(path, "rb");
    if (file == NULL) {
        return -1;
    }
    sha256_init(&context);
    while ((count = fread(input, 1U, sizeof(input), file)) != 0U) {
        sha256_update(&context, input, count);
    }
    if (ferror(file) != 0 || fclose(file) != 0) {
        return -1;
    }
    sha256_final(&context, digest);
    for (i = 0U; i < 32U; ++i) {
        (void)snprintf(&output[i * 2U], 3U, "%02x", digest[i]);
    }
    output[64] = '\0';
    return 0;
}

static int metadata_contains_canonical_identity(const char *path)
{
    char metadata[8192];
    FILE *file = fopen(path, "rb");
    size_t count;

    if (file == NULL) {
        return -1;
    }
    count = fread(metadata, 1U, sizeof(metadata) - 1U, file);
    if (ferror(file) != 0 || count == sizeof(metadata) - 1U ||
        fclose(file) != 0) {
        return -1;
    }
    metadata[count] = '\0';
    if (strstr(metadata, "\"model_size_bytes\": 164") == NULL ||
        strstr(metadata, "\"model_sha256\": \"" FROZEN_MODEL_SHA256
               "\"") == NULL ||
        strstr(metadata, "\"model_version\": 731924617") == NULL) {
        return -1;
    }
    return 0;
}

int contest_validate_mlp_identity(const struct contest_runtime_config *config,
                                  char *error, size_t error_capacity)
{
    struct stat model_stat;
    char actual_sha256[65];

    if (config == NULL || config->mode != CONTEST_RUNTIME_MLP) {
        return 0;
    }
    if (stat(config->model_path, &model_stat) != 0 ||
        !S_ISREG(model_stat.st_mode) ||
        (unsigned long long)model_stat.st_size != FROZEN_MODEL_SIZE) {
        set_error(error, error_capacity, "MLP model size or file type is invalid");
        return -1;
    }
    if (sha256_file(config->model_path, actual_sha256) != 0 ||
        strcmp(actual_sha256, FROZEN_MODEL_SHA256) != 0) {
        set_error(error, error_capacity, "MLP model SHA-256 does not match frozen identity");
        return -1;
    }
    if (metadata_contains_canonical_identity(config->metadata_path) != 0) {
        set_error(error, error_capacity, "MLP metadata identity is invalid");
        return -1;
    }
    return 0;
}
