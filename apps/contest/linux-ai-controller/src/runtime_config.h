#ifndef CONTEST_RUNTIME_CONFIG_H
#define CONTEST_RUNTIME_CONFIG_H

#include <stddef.h>
#include <stdint.h>

#define CONTEST_RUNTIME_TEXT_MAX 256U

enum contest_runtime_mode {
    CONTEST_RUNTIME_L3_SMOKE = 0,
    CONTEST_RUNTIME_UDP_ECHO,
    CONTEST_RUNTIME_TCP_CLIENT,
    CONTEST_RUNTIME_UDP_RELIABILITY,
    CONTEST_RUNTIME_TCP_RELIABILITY,
    CONTEST_RUNTIME_ICPC_RELIABILITY,
    CONTEST_RUNTIME_ICPC,
    CONTEST_RUNTIME_ICPC_SAFE,
    CONTEST_RUNTIME_AI_VERIFY,
    CONTEST_RUNTIME_FIXED,
    CONTEST_RUNTIME_MLP,
};

struct contest_runtime_config {
    enum contest_runtime_mode mode;
    char mode_text[24];
    char bind_ip[16];
    char peer_ip[16];
    uint16_t udp_port;
    uint16_t tcp_port;
    char model_path[CONTEST_RUNTIME_TEXT_MAX];
    char metadata_path[CONTEST_RUNTIME_TEXT_MAX];
    char run_id[CONTEST_RUNTIME_TEXT_MAX];
    char event_log[CONTEST_RUNTIME_TEXT_MAX];
    uint32_t session_id;
    uint32_t seed;
    int ticks;
    unsigned present;
};

int contest_parse_runtime_config(int argc, char **argv,
                                 struct contest_runtime_config *config,
                                 char *error, size_t error_capacity);

int contest_validate_runtime_config(const struct contest_runtime_config *config,
                                    char *error, size_t error_capacity);

/* MLP startup validation. FIXED deliberately does not read or invoke model
 * bytes; rootfs preparation still binds the identical canonical artifacts. */
int contest_validate_mlp_identity(const struct contest_runtime_config *config,
                                  char *error, size_t error_capacity);

#endif /* CONTEST_RUNTIME_CONFIG_H */
