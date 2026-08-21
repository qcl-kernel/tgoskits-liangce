#include "watchdog.h"

#include <stdio.h>

static int expect_status(enum watchdog_status actual,
                         enum watchdog_status expected,
                         const char *description)
{
    if (actual == expected) {
        return 0;
    }
    fprintf(stderr, "%s: got %d, expected %d\n", description, actual,
            expected);
    return 1;
}

static int test_watchdog_boundaries(void)
{
    struct watchdog watchdog;

    watchdog_init(&watchdog);
    if (expect_status(watchdog_poll(&watchdog, 0U), WATCHDOG_STATUS_SAFE,
                      "boot safe") != 0 ||
        !watchdog_safe_pending(&watchdog)) {
        return 1;
    }
    if (expect_status(watchdog_begin_session(&watchdog, 11U, 0U),
                      WATCHDOG_STATUS_ALIVE, "session start") != 0 ||
        expect_status(watchdog_refresh_apply(&watchdog, 11U, 1U, 1U,
                                             WATCHDOG_VALIDITY_MS, 0U),
                      WATCHDOG_STATUS_ALIVE, "first apply") != 0) {
        return 1;
    }
    if (expect_status(watchdog_poll(&watchdog, UINT64_C(499000000)),
                      WATCHDOG_STATUS_ALIVE, "499 ms") != 0 ||
        watchdog_safe_pending(&watchdog)) {
        return 1;
    }
    if (expect_status(watchdog_poll(&watchdog, WATCHDOG_TIMEOUT_NS),
                      WATCHDOG_STATUS_EXPIRED, "500 ms") != 0 ||
        !watchdog_safe_pending(&watchdog)) {
        return 1;
    }
    if (expect_status(watchdog_poll(&watchdog, UINT64_C(501000000)),
                      WATCHDOG_STATUS_EXPIRED, "501 ms") != 0) {
        return 1;
    }
    return 0;
}

static int test_only_new_valid_apply_refreshes(void)
{
    struct watchdog watchdog;

    watchdog_init(&watchdog);
    if (watchdog_begin_session(&watchdog, 21U, 0U) !=
        WATCHDOG_STATUS_ALIVE) {
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 21U, 1U, 1U, 499U, 0U) !=
        WATCHDOG_STATUS_INVALID_VALIDITY) {
        fprintf(stderr, "validity 499 refreshed watchdog\n");
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 21U, 1U, 1U, 501U, 1U) !=
        WATCHDOG_STATUS_INVALID_VALIDITY) {
        fprintf(stderr, "validity 501 refreshed watchdog\n");
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 21U, 1U, 1U,
                               WATCHDOG_VALIDITY_MS, 2U) !=
        WATCHDOG_STATUS_ALIVE) {
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 21U, 1U, 1U,
                               WATCHDOG_VALIDITY_MS, 100U) !=
        WATCHDOG_STATUS_DUPLICATE) {
        fprintf(stderr, "duplicate apply refreshed watchdog\n");
        return 1;
    }
    if (watchdog_poll(&watchdog, UINT64_C(500000002)) !=
        WATCHDOG_STATUS_EXPIRED) {
        fprintf(stderr, "duplicate apply kept watchdog alive\n");
        return 1;
    }

    if (watchdog_refresh_apply(&watchdog, 21U, 2U, 2U,
                               WATCHDOG_VALIDITY_MS, UINT64_C(500000002)) !=
        WATCHDOG_STATUS_RECOVERY_REQUIRES_SESSION ||
        !watchdog_safe_pending(&watchdog)) {
        fprintf(stderr, "old session recovered watchdog\n");
        return 1;
    }
    if (watchdog_begin_session(&watchdog, 22U, UINT64_C(500000002)) !=
            WATCHDOG_STATUS_ALIVE ||
        watchdog_refresh_apply(&watchdog, 22U, 1U, 1U,
                               WATCHDOG_VALIDITY_MS, UINT64_C(500000002)) !=
            WATCHDOG_STATUS_ALIVE ||
        watchdog_safe_pending(&watchdog)) {
        fprintf(stderr, "new session did not recover watchdog\n");
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 22U, 1U, 2U,
                               WATCHDOG_VALIDITY_MS, UINT64_C(500000003)) !=
        WATCHDOG_STATUS_STALE_SEQUENCE) {
        fprintf(stderr, "stale apply was accepted\n");
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 22U, 3U, 1U,
                               WATCHDOG_VALIDITY_MS, UINT64_C(500000004)) !=
        WATCHDOG_STATUS_STALE_REQUEST) {
        fprintf(stderr, "stale request was accepted\n");
        return 1;
    }
    return 0;
}

static int test_session_and_clock_boundaries(void)
{
    struct watchdog watchdog;

    watchdog_init(&watchdog);
    if (watchdog_begin_session(&watchdog, 31U, 100U) !=
        WATCHDOG_STATUS_ALIVE) {
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 30U, 1U, 1U,
                               WATCHDOG_VALIDITY_MS, 101U) !=
        WATCHDOG_STATUS_SESSION_MISMATCH) {
        fprintf(stderr, "old session refreshed watchdog\n");
        return 1;
    }
    if (watchdog_begin_session(&watchdog, 32U, 200U) !=
        WATCHDOG_STATUS_ALIVE ||
        watchdog_safe_pending(&watchdog) == 0U) {
        fprintf(stderr, "new session did not return safe\n");
        return 1;
    }
    if (watchdog_begin_session(&watchdog, 31U, 201U) !=
        WATCHDOG_STATUS_SESSION_NOT_NEW) {
        fprintf(stderr, "retired session was reused\n");
        return 1;
    }
    if (watchdog_refresh_apply(&watchdog, 32U, 1U, 1U,
                               WATCHDOG_VALIDITY_MS, 199U) !=
        WATCHDOG_STATUS_CLOCK_BACKWARD ||
        !watchdog_safe_pending(&watchdog)) {
        fprintf(stderr, "backward monotonic time was accepted\n");
        return 1;
    }
    return 0;
}

int main(void)
{
    if (test_watchdog_boundaries() != 0 ||
        test_only_new_valid_apply_refreshes() != 0 ||
        test_session_and_clock_boundaries() != 0) {
        return 1;
    }
    puts("WATCHDOG_HOST_PASS");
    return 0;
}
