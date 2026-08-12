/*
 * One-shot Linux Guest O_DIRECT virtio-blk read probe.
 *
 * This helper is intentionally a user-space observation tool, not a raw
 * virtio descriptor tool.  It proves only that the Guest issued one
 * O_DIRECT pread(2) into this process's pinned buffer.  In particular, its
 * READY/DONE records do not prove descriptor addressing, a stage-2 mapping,
 * a hardware DMA engine, or DMA isolation.
 *
 * The caller must mount the root filesystem before starting this program.
 * After READY, the helper accepts only the exact LF-terminated authorization
 * line `GO <nonce>\n` from one independent virtio-console character device.
 * It never reads standard input. CRLF, EOF, a mismatched nonce, and trailing
 * bytes fail closed before pread(2).  `--hold-ms` bounds that authorization
 * wait.
 * Arguments are deliberately narrow: there is no address argument and the
 * selected block device is opened read-only.  The process fails closed before
 * READY if it cannot pin one page and derive a present PFN from
 * /proc/self/pagemap.  `gpa` is only that page's Linux pagemap PFN-derived
 * address; a host runner must bind it to its own stage-2/HPA evidence.
 *
 * Build for the disposable Guest rootfs, for example:
 *   ${CC:-gcc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic \
 *       -static scripts/contest/guest_virtio_blk_odirect_probe.c \
 *       -o guest-virtio-blk-odirect-probe
 *
 * Usage:
 *   guest-virtio-blk-odirect-probe --nonce <32-lower-hex> --device <block> \
 *       --control-device /dev/hvc0 --sector <decimal> --hold-ms <decimal>
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

enum {
    BLOCK_BYTES = 512,
    NONCE_HEX_BYTES = 32,
    PAGEMAP_ENTRY_BYTES = 8,
    PAGEMAP_PFN_BITS = 55,
    MAX_HOLD_MILLISECONDS = 600000,
};

#define PAGEMAP_PRESENT (UINT64_C(1) << 63)
#define PAGEMAP_SWAPPED (UINT64_C(1) << 62)
#define PAGEMAP_PFN_MASK ((UINT64_C(1) << PAGEMAP_PFN_BITS) - UINT64_C(1))

struct probe_arguments {
    const char *nonce;
    const char *device;
    const char *control_device;
    uint64_t sector;
    uint64_t hold_milliseconds;
};

static int run_probe(const struct probe_arguments *arguments);
static int parse_arguments(int argc, char **argv, struct probe_arguments *arguments);
static int parse_decimal_u64(const char *text, uint64_t maximum, uint64_t *value);
static bool is_lower_hex_nonce(const char *nonce);
static bool is_safe_device_path(const char *device);
static bool is_control_device_path(const char *device);
static int allocate_and_pin_buffer(size_t page_size, void **buffer);
static int read_buffer_gpa(const void *buffer, size_t page_size, uint64_t *gpa);
static int open_read_only_block_device(const char *device);
static int open_read_only_control_device(const char *device);
static int read_exact_sector(int device_fd, void *buffer, uint64_t sector);
static int wait_for_go_authorization(int control_fd, const char *nonce, uint64_t timeout_milliseconds);
static int remaining_wait_milliseconds(const struct timespec *deadline, int *milliseconds);
static void print_usage(const char *program);

int main(int argc, char **argv)
{
    struct probe_arguments arguments;

    if (parse_arguments(argc, argv, &arguments) != 0) {
        print_usage(argv[0]);
        return EXIT_FAILURE;
    }
    return run_probe(&arguments) == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int run_probe(const struct probe_arguments *arguments)
{
    const long configured_page_size = sysconf(_SC_PAGESIZE);
    void *buffer = NULL;
    uint64_t gpa = 0;
    int device_fd = -1;
    int control_fd = -1;
    int result = -1;

    if (configured_page_size <= 0 || (configured_page_size & (configured_page_size - 1)) != 0
        || configured_page_size < BLOCK_BYTES) {
        fprintf(stderr, "guest virtio-blk probe: unsupported page size\n");
        return -1;
    }

    const size_t page_size = (size_t)configured_page_size;
    if (allocate_and_pin_buffer(page_size, &buffer) != 0
        || read_buffer_gpa(buffer, page_size, &gpa) != 0) {
        goto cleanup;
    }

    device_fd = open_read_only_block_device(arguments->device);
    if (device_fd < 0) {
        goto cleanup;
    }
    control_fd = open_read_only_control_device(arguments->control_device);
    if (control_fd < 0) {
        goto cleanup;
    }

    printf(
        "AXVISOR_VIRTIO_BLK_ODIRECT_READY nonce=%s gpa=0x%" PRIx64
        " size=%d sector=%" PRIu64 " device=%s control_device=%s\n",
        arguments->nonce,
        gpa,
        BLOCK_BYTES,
        arguments->sector,
        arguments->device,
        arguments->control_device
    );
    if (fflush(stdout) != 0) {
        fprintf(stderr, "guest virtio-blk probe: cannot flush READY marker: %s\n", strerror(errno));
        goto cleanup;
    }
    if (wait_for_go_authorization(control_fd, arguments->nonce, arguments->hold_milliseconds) != 0) {
        goto cleanup;
    }

    if (read_exact_sector(device_fd, buffer, arguments->sector) != 0) {
        goto cleanup;
    }

    printf(
        "AXVISOR_VIRTIO_BLK_ODIRECT_DONE nonce=%s bytes=%d gpa=0x%" PRIx64 "\n",
        arguments->nonce,
        BLOCK_BYTES,
        gpa
    );
    if (fflush(stdout) != 0) {
        fprintf(stderr, "guest virtio-blk probe: cannot flush DONE marker: %s\n", strerror(errno));
        goto cleanup;
    }

    /* The runner terminates this disposable Guest after its post-DONE capture. */
    for (;;) {
        pause();
    }

cleanup:
    if (control_fd >= 0) {
        (void)close(control_fd);
    }
    if (device_fd >= 0) {
        (void)close(device_fd);
    }
    if (buffer != NULL) {
        (void)munlock(buffer, page_size);
        free(buffer);
    }
    return result;
}

static int parse_arguments(int argc, char **argv, struct probe_arguments *arguments)
{
    if (argc != 11 || strcmp(argv[1], "--nonce") != 0 || strcmp(argv[3], "--device") != 0
        || strcmp(argv[5], "--control-device") != 0 || strcmp(argv[7], "--sector") != 0
        || strcmp(argv[9], "--hold-ms") != 0) {
        fprintf(stderr, "guest virtio-blk probe: invalid argument layout\n");
        return -1;
    }
    if (!is_lower_hex_nonce(argv[2])) {
        fprintf(stderr, "guest virtio-blk probe: nonce must be 32 lower-case hexadecimal characters\n");
        return -1;
    }
    if (!is_safe_device_path(argv[4])) {
        fprintf(stderr, "guest virtio-blk probe: device must be a safe /dev path\n");
        return -1;
    }
    if (!is_control_device_path(argv[6])) {
        fprintf(stderr, "guest virtio-blk probe: control device must be /dev/hvc0\n");
        return -1;
    }
    if (parse_decimal_u64(argv[8], UINT64_MAX, &arguments->sector) != 0
        || parse_decimal_u64(argv[10], MAX_HOLD_MILLISECONDS, &arguments->hold_milliseconds) != 0) {
        fprintf(stderr, "guest virtio-blk probe: sector or hold-ms is invalid\n");
        return -1;
    }
    arguments->nonce = argv[2];
    arguments->device = argv[4];
    arguments->control_device = argv[6];
    return 0;
}

static int parse_decimal_u64(const char *text, uint64_t maximum, uint64_t *value)
{
    char *end = NULL;
    unsigned long long parsed = 0;

    if (text == NULL || *text == '\0') {
        return -1;
    }
    for (const char *cursor = text; *cursor != '\0'; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            return -1;
        }
    }
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if (errno == ERANGE || end == NULL || *end != '\0' || parsed > maximum) {
        return -1;
    }
    *value = (uint64_t)parsed;
    return 0;
}

static bool is_lower_hex_nonce(const char *nonce)
{
    if (nonce == NULL || strlen(nonce) != NONCE_HEX_BYTES) {
        return false;
    }
    for (size_t index = 0; index < NONCE_HEX_BYTES; ++index) {
        const char character = nonce[index];
        if (!((character >= '0' && character <= '9')
              || (character >= 'a' && character <= 'f'))) {
            return false;
        }
    }
    return true;
}

static bool is_safe_device_path(const char *device)
{
    const char prefix[] = "/dev/";

    if (device == NULL || strncmp(device, prefix, sizeof(prefix) - 1) != 0
        || device[sizeof(prefix) - 1] == '\0') {
        return false;
    }
    for (const char *cursor = device; *cursor != '\0'; ++cursor) {
        const char character = *cursor;
        if (!((character >= 'a' && character <= 'z')
              || (character >= 'A' && character <= 'Z')
              || (character >= '0' && character <= '9') || character == '/'
              || character == '_' || character == '-')) {
            return false;
        }
    }
    return true;
}

static bool is_control_device_path(const char *device)
{
    return device != NULL && strcmp(device, "/dev/hvc0") == 0;
}

static int allocate_and_pin_buffer(size_t page_size, void **buffer)
{
    void *allocated = NULL;

    if (posix_memalign(&allocated, page_size, page_size) != 0) {
        fprintf(stderr, "guest virtio-blk probe: posix_memalign failed\n");
        return -1;
    }
    memset(allocated, 0xa5, page_size);
    ((volatile unsigned char *)allocated)[0] = 0xa5;
    if (mlock(allocated, page_size) != 0) {
        fprintf(stderr, "guest virtio-blk probe: mlock failed: %s\n", strerror(errno));
        free(allocated);
        return -1;
    }
    *buffer = allocated;
    return 0;
}

static int read_buffer_gpa(const void *buffer, size_t page_size, uint64_t *gpa)
{
    const uint64_t virtual_page = (uint64_t)(uintptr_t)buffer / page_size;
    const uint64_t maximum_offset = (uint64_t)INT64_MAX;
    uint64_t entry_offset = 0;
    uint64_t entry = 0;
    uint64_t pfn = 0;
    int pagemap_fd = -1;
    ssize_t bytes_read = 0;

    if (virtual_page > UINT64_MAX / PAGEMAP_ENTRY_BYTES || sizeof(entry) != PAGEMAP_ENTRY_BYTES) {
        fprintf(stderr, "guest virtio-blk probe: pagemap offset overflow\n");
        return -1;
    }
    entry_offset = virtual_page * PAGEMAP_ENTRY_BYTES;
    if (entry_offset > maximum_offset) {
        fprintf(stderr, "guest virtio-blk probe: pagemap offset exceeds off_t\n");
        return -1;
    }
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
    if (pagemap_fd < 0) {
        fprintf(stderr, "guest virtio-blk probe: cannot open pagemap: %s\n", strerror(errno));
        return -1;
    }
    bytes_read = pread(pagemap_fd, &entry, sizeof(entry), (off_t)entry_offset);
    (void)close(pagemap_fd);
    if (bytes_read != (ssize_t)sizeof(entry)) {
        fprintf(stderr, "guest virtio-blk probe: pagemap entry is incomplete\n");
        return -1;
    }
    if ((entry & PAGEMAP_PRESENT) == 0 || (entry & PAGEMAP_SWAPPED) != 0) {
        fprintf(stderr, "guest virtio-blk probe: buffer page is not present\n");
        return -1;
    }
    pfn = entry & PAGEMAP_PFN_MASK;
    if (pfn == 0 || pfn > UINT64_MAX / page_size) {
        fprintf(stderr, "guest virtio-blk probe: pagemap PFN is unavailable or overflows\n");
        return -1;
    }
    *gpa = pfn * page_size;
    return 0;
}

static int open_read_only_block_device(const char *device)
{
    struct stat device_status;
    const int descriptor = open(device, O_RDONLY | O_DIRECT | O_CLOEXEC);

    if (descriptor < 0) {
        fprintf(stderr, "guest virtio-blk probe: cannot open %s: %s\n", device, strerror(errno));
        return -1;
    }
    if (fstat(descriptor, &device_status) != 0 || !S_ISBLK(device_status.st_mode)) {
        fprintf(stderr, "guest virtio-blk probe: %s is not a block device\n", device);
        (void)close(descriptor);
        return -1;
    }
    return descriptor;
}

static int open_read_only_control_device(const char *device)
{
    struct stat device_status;
    const int descriptor = open(device, O_RDONLY | O_NONBLOCK | O_NOCTTY | O_CLOEXEC);

    if (descriptor < 0) {
        fprintf(stderr, "guest virtio-blk probe: cannot open control device %s: %s\n", device, strerror(errno));
        return -1;
    }
    if (fstat(descriptor, &device_status) != 0 || !S_ISCHR(device_status.st_mode)) {
        fprintf(stderr, "guest virtio-blk probe: control device %s is not a character device\n", device);
        (void)close(descriptor);
        return -1;
    }
    return descriptor;
}

static int read_exact_sector(int device_fd, void *buffer, uint64_t sector)
{
    const uint64_t maximum_sector = (uint64_t)INT64_MAX / BLOCK_BYTES;
    off_t offset = 0;
    ssize_t bytes_read = 0;

    if (sector > maximum_sector) {
        fprintf(stderr, "guest virtio-blk probe: sector offset overflows off_t\n");
        return -1;
    }
    offset = (off_t)(sector * BLOCK_BYTES);
    bytes_read = pread(device_fd, buffer, BLOCK_BYTES, offset);
    if (bytes_read != BLOCK_BYTES) {
        fprintf(stderr, "guest virtio-blk probe: expected one sector, read %zd: %s\n", bytes_read, strerror(errno));
        return -1;
    }
    return 0;
}

static int wait_for_go_authorization(int control_fd, const char *nonce, uint64_t timeout_milliseconds)
{
    enum {
        GO_PREFIX_BYTES = 3,
        GO_SUFFIX_BYTES = 1,
        GO_MESSAGE_BYTES = GO_PREFIX_BYTES + NONCE_HEX_BYTES + GO_SUFFIX_BYTES,
    };
    char expected[GO_MESSAGE_BYTES + 1];
    char received[GO_MESSAGE_BYTES];
    const int expected_length = snprintf(expected, sizeof(expected), "GO %s\n", nonce);
    struct timespec deadline;
    size_t received_bytes = 0;

    if (expected_length != (int)sizeof(received) || clock_gettime(CLOCK_MONOTONIC, &deadline) != 0) {
        fprintf(stderr, "guest virtio-blk probe: cannot initialize GO authorization\n");
        return -1;
    }
    deadline.tv_sec += (time_t)(timeout_milliseconds / 1000);
    deadline.tv_nsec += (long)((timeout_milliseconds % 1000) * 1000000);
    if (deadline.tv_nsec >= 1000000000L) {
        ++deadline.tv_sec;
        deadline.tv_nsec -= 1000000000L;
    }

    while (received_bytes < sizeof(received)) {
        struct pollfd input = {
            .fd = control_fd,
            .events = POLLIN,
            .revents = 0,
        };
        int remaining_milliseconds = 0;
        int poll_result = 0;
        ssize_t bytes_read = 0;

        if (remaining_wait_milliseconds(&deadline, &remaining_milliseconds) != 0) {
            fprintf(stderr, "guest virtio-blk probe: GO authorization timed out\n");
            return -1;
        }
        poll_result = poll(&input, 1, remaining_milliseconds);
        if (poll_result == 0) {
            fprintf(stderr, "guest virtio-blk probe: GO authorization timed out\n");
            return -1;
        }
        if (poll_result < 0) {
            if (errno == EINTR) {
                continue;
            }
            fprintf(stderr, "guest virtio-blk probe: cannot poll GO authorization: %s\n", strerror(errno));
            return -1;
        }
        if ((input.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0 || (input.revents & POLLIN) == 0) {
            fprintf(stderr, "guest virtio-blk probe: GO authorization input closed or invalid\n");
            return -1;
        }
        bytes_read = read(control_fd, received + received_bytes, sizeof(received) - received_bytes);
        if (bytes_read < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            continue;
        }
        if (bytes_read <= 0) {
            fprintf(stderr, "guest virtio-blk probe: GO authorization reached EOF or failed\n");
            return -1;
        }
        received_bytes += (size_t)bytes_read;
    }

    if (memcmp(received, expected, sizeof(received)) != 0) {
        fprintf(stderr, "guest virtio-blk probe: GO authorization does not match nonce\n");
        return -1;
    }

    struct pollfd extra_input = {
        .fd = control_fd,
        .events = POLLIN,
        .revents = 0,
    };
    const int extra_poll = poll(&extra_input, 1, 0);
    if (extra_poll != 0) {
        fprintf(stderr, "guest virtio-blk probe: GO authorization has trailing bytes or EOF\n");
        return -1;
    }
    return 0;
}

static int remaining_wait_milliseconds(const struct timespec *deadline, int *milliseconds)
{
    struct timespec now;
    time_t seconds = 0;
    long nanoseconds = 0;
    uint64_t total_milliseconds = 0;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1;
    }
    if (now.tv_sec > deadline->tv_sec
        || (now.tv_sec == deadline->tv_sec && now.tv_nsec >= deadline->tv_nsec)) {
        return -1;
    }
    seconds = deadline->tv_sec - now.tv_sec;
    nanoseconds = deadline->tv_nsec - now.tv_nsec;
    if (nanoseconds < 0) {
        --seconds;
        nanoseconds += 1000000000L;
    }
    total_milliseconds = (uint64_t)seconds * 1000 + (uint64_t)(nanoseconds + 999999L) / 1000000;
    if (total_milliseconds == 0 || total_milliseconds > INT_MAX) {
        return -1;
    }
    *milliseconds = (int)total_milliseconds;
    return 0;
}

static void print_usage(const char *program)
{
    fprintf(
        stderr,
        "usage: %s --nonce <32-lower-hex> --device <block-device> --control-device /dev/hvc0 "
        "--sector <decimal> --hold-ms <0..600000>\n",
        program
    );
}
