# Contest Linux guest application (P4 network skeleton).
#
# Targets the locked aarch64-linux-musl toolchain and produces a static
# binary for the disposable contest rootfs.

CC ?= aarch64-linux-musl-gcc
CFLAGS ?= -O2 -Wall -Wextra -Werror -std=c11 -static -D_GNU_SOURCE
BUILD_DIR := build

SRCS := src/main.c src/icpc/icpc.c
OBJS := $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(SRCS))
BIN := $(BUILD_DIR)/linux-ai-controller

.PHONY: all clean p4-network

all: $(BIN)

p4-network: $(BIN)

$(BIN): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/%.o: src/%.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD_DIR)/icpc/%.o: src/icpc/%.c | $(BUILD_DIR)/icpc
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD_DIR)/icpc:
	mkdir -p $(BUILD_DIR)/icpc

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

clean:
	rm -rf $(BUILD_DIR)
