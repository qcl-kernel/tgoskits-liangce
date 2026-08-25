#include <assert.h>
#include <stdio.h>
#include <stdint.h>

#include "control_loop.h"

static struct control_mailbox_command command(uint32_t session_id,
                                              uint32_t sequence,
                                              uint32_t request_id,
                                              int32_t duty_q16_16)
{
    return (struct control_mailbox_command){
        .session_id = session_id,
        .sequence = sequence,
        .request_id = request_id,
        .command = CONTROL_MAILBOX_APPLY_OUTPUT,
        .control_mode = CONTROL_MAILBOX_FIXED_BASELINE,
        .duty_q16_16 = duty_q16_16,
        .target_mC = CONTROL_MAILBOX_TARGET_MC,
        .model_version = 0U,
        .validity_ms = CONTROL_MAILBOX_VALIDITY_MS,
    };
}

int main(void)
{
    struct control_loop loop;
    struct control_loop_status_snapshot snapshot;
    struct control_mailbox_command first;
    struct control_mailbox_command stale;
    struct control_mailbox_command recovery;

    control_loop_init(&loop, 0U);
    assert(control_loop_release(&loop, 0U, &snapshot) ==
           CONTROL_LOOP_RESULT_NOT_DUE);
    assert(control_loop_release(&loop, UINT64_C(50000000), &snapshot) ==
           CONTROL_LOOP_RESULT_NOT_DUE);

    assert(control_loop_begin_session(&loop, 0U, UINT64_C(50000000)) ==
           CONTROL_LOOP_RESULT_INVALID_SESSION);
    assert(control_loop_begin_session(&loop, 1U, UINT64_C(50000000)) ==
           CONTROL_LOOP_RESULT_OK);
    assert(control_loop_begin_session(&loop, 1U, UINT64_C(50000000)) ==
           CONTROL_LOOP_RESULT_SESSION_NOT_NEW);

    first = command(1U, 1U, 1U, 32768);
    assert(control_loop_publish(&loop, &first) == CONTROL_MAILBOX_STATUS_OK);
    assert(control_loop_release(&loop, UINT64_C(150000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(snapshot.sample_index == 0U);
    assert(snapshot.session_id == 1U);
    assert(snapshot.sequence == 1U);
    assert(snapshot.request_id == 1U);
    assert(snapshot.duty_q16_16 == 32768);
    assert(snapshot.safe == 0U);

    assert(control_loop_publish(&loop, &first) ==
           CONTROL_MAILBOX_STATUS_DUPLICATE);
    assert(control_loop_release(&loop, UINT64_C(250000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(snapshot.sample_index == 1U);
    assert(snapshot.sequence == 1U);

    stale = command(1U, 1U, 2U, 32768);
    assert(control_loop_publish(&loop, &stale) != CONTROL_MAILBOX_STATUS_OK);

    assert(control_loop_release(&loop, UINT64_C(350000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(control_loop_release(&loop, UINT64_C(450000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(control_loop_release(&loop, UINT64_C(550000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(control_loop_release(&loop, UINT64_C(650000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(snapshot.sample_index == 5U);
    assert(snapshot.safe != 0U);
    assert((snapshot.health_flags & CONTROL_LOOP_HEALTH_NETWORK_TIMEOUT) != 0U);

    assert(control_loop_begin_session(&loop, 2U, UINT64_C(650000000)) ==
           CONTROL_LOOP_RESULT_OK);
    assert(control_loop_begin_session(&loop, 2U, UINT64_C(650000000)) ==
           CONTROL_LOOP_RESULT_SESSION_NOT_NEW);
    assert(control_loop_begin_session(&loop, 1U, UINT64_C(650000000)) ==
           CONTROL_LOOP_RESULT_SESSION_NOT_NEW);
    recovery = command(2U, 1U, 1U, 65536);
    assert(control_loop_publish(&loop, &recovery) == CONTROL_MAILBOX_STATUS_OK);
    assert(control_loop_release(&loop, UINT64_C(750000000), &snapshot) ==
           CONTROL_LOOP_RESULT_OK);
    assert(snapshot.sample_index == 6U);
    assert(snapshot.session_id == 2U);
    assert(snapshot.safe == 0U);

    assert(control_loop_release(&loop, UINT64_C(700000000), &snapshot) ==
           CONTROL_LOOP_RESULT_CLOCK_BACKWARD);
    puts("CONTROL_LOOP_HOST_PASS");
    return 0;
}
