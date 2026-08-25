#include "../src/runtime_config.h"

#include <stdio.h>
#include <string.h>

static int expect_valid(void)
{
    char *argv[] = {
        (char *)"linux-ai-controller", (char *)"--mode", (char *)"mlp",
        (char *)"--bind", (char *)"10.77.0.1", (char *)"--peer",
        (char *)"10.77.0.2", (char *)"--udp-port=46000",
        (char *)"--tcp-port", (char *)"46001", (char *)"--model",
        (char *)"/opt/tgos/model.bin", (char *)"--metadata",
        (char *)"/opt/tgos/metadata.json", (char *)"--run-id",
        (char *)"phase5-ai-mlp-s43-test", (char *)"--event-log",
        (char *)"/run/tgos/linux-events.jsonl", (char *)"--session-id=4",
        (char *)"--seed=43",
    };
    struct contest_runtime_config config;
    char error[128];

    if (contest_parse_runtime_config((int)(sizeof(argv) / sizeof(argv[0])),
                                     argv, &config, error, sizeof(error)) != 0 ||
        config.mode != CONTEST_RUNTIME_MLP || config.ticks != 1800 ||
        config.seed != 43U) {
        fprintf(stderr, "valid CLI rejected: %s\n", error);
        return 1;
    }
    return 0;
}

static int expect_rejected(char **argv, int argc)
{
    struct contest_runtime_config config;
    char error[128];

    return contest_parse_runtime_config(argc, argv, &config, error,
                                        sizeof(error)) == 0;
}

static int expect_canonical_model(const char *model_path,
                                  const char *metadata_path)
{
    struct contest_runtime_config config;
    char error[128];

    memset(&config, 0, sizeof(config));
    config.mode = CONTEST_RUNTIME_MLP;
    (void)snprintf(config.model_path, sizeof(config.model_path), "%s",
                   model_path);
    (void)snprintf(config.metadata_path, sizeof(config.metadata_path), "%s",
                   metadata_path);
    if (contest_validate_mlp_identity(&config, error, sizeof(error)) != 0) {
        fprintf(stderr, "canonical model rejected: %s\n", error);
        return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *model_path = argc > 1 ? argv[1] : "model/model.bin";
    const char *metadata_path = argc > 2 ? argv[2] : "model/metadata.json";
    char *drift[] = {
        (char *)"linux-ai-controller", (char *)"--mode=mlp",
        (char *)"--bind=10.77.0.1", (char *)"--peer=10.77.0.3",
        (char *)"--udp-port=46000", (char *)"--tcp-port=46001",
        (char *)"--model=/opt/tgos/model.bin",
        (char *)"--metadata=/opt/tgos/metadata.json",
        (char *)"--run-id=phase5-ai-mlp-s43-test",
        (char *)"--event-log=/run/tgos/linux-events.jsonl",
        (char *)"--session-id=4", (char *)"--seed=43",
    };
    char *missing[] = {
        (char *)"linux-ai-controller", (char *)"--mode=mlp",
        (char *)"--bind=10.77.0.1", (char *)"--peer=10.77.0.2",
        (char *)"--udp-port=46000", (char *)"--tcp-port=46001",
        (char *)"--model=/opt/tgos/model.bin",
        (char *)"--metadata=/opt/tgos/metadata.json",
        (char *)"--run-id=phase5-ai-mlp-s43-test",
        (char *)"--session-id=4", (char *)"--seed=43",
    };
    char *unknown[] = {(char *)"linux-ai-controller", (char *)"--silent"};

    if (expect_valid() != 0 ||
        expect_canonical_model(model_path, metadata_path) != 0 ||
        expect_rejected(drift, (int)(sizeof(drift) / sizeof(drift[0]))) != 0 ||
        expect_rejected(missing, (int)(sizeof(missing) / sizeof(missing[0]))) != 0 ||
        expect_rejected(unknown, (int)(sizeof(unknown) / sizeof(unknown[0]))) != 0) {
        return 1;
    }
    puts("LINUX_CONTROLLER_CLI_HOST_PASS");
    return 0;
}
