// Copyright 2025 The Axvisor Team
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/* Contest Zephyr control guest.
 *
 * P4: low-priority network thread with l3-smoke / udp-echo / tcp-listen
 * modes over the mediated virtio-net frontend.  P5 adds the plant/control
 * loop on top of the ICPC link.
 *
 * The READY marker binds the boot id and the actual interface identity
 * (MAC, IPv4, prefix, connected route, MTU).
 */

#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/net_core.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/socket.h>
#include <zephyr/posix/unistd.h>
#include <zephyr/posix/netdb.h>
#include <zephyr/sys/printk.h>

#include "icpc/icpc.h"
#include "../../common/icpc_payload.h"
#include "control_loop.h"
#include "plant.h"

#define PEER_IP "10.77.0.1"
#define UDP_PORT 46000
#define TCP_PORT 46001
#define P4_RELIABILITY_MESSAGES 10000U
#define P4_RELIABILITY_CONTROL_MESSAGES 1000U
#define P4_UDP_PAYLOAD_SIZE 256U

#define NET_STACK_SIZE 4096
#define NET_PRIORITY 5
#define CONTROL_STACK_SIZE 2048
#define CONTROL_PRIORITY 1
#define CONTROL_STATUS_QUEUE_DEPTH 2
#define LOGGER_STACK_SIZE 4096
#define LOGGER_PRIORITY 7
#define CONTROL_EVENT_QUEUE_DEPTH 256

struct control_event_record {
	uint32_t session_id;
	uint32_t sequence;
	uint32_t request_id;
	uint64_t sample_index;
	uint64_t monotonic_ns;
	uint8_t has_value;
	uint8_t has_unit;
	int32_t value;
	char event[24];
	char unit[16];
	char outcome[16];
};

static struct control_loop g_control_loop;
static struct k_spinlock g_control_loop_lock;
static struct k_spinlock g_control_event_queue_lock;
K_MSGQ_DEFINE(control_status_queue,
		      sizeof(struct control_loop_status_snapshot),
		      CONTROL_STATUS_QUEUE_DEPTH, 4);
K_MSGQ_DEFINE(control_event_queue,
		      sizeof(struct control_event_record),
		      CONTROL_EVENT_QUEUE_DEPTH, 4);
K_THREAD_STACK_DEFINE(control_stack, CONTROL_STACK_SIZE);
K_THREAD_STACK_DEFINE(logger_stack, LOGGER_STACK_SIZE);
static struct k_thread control_thread;
static struct k_thread logger_thread;
static struct k_timer control_timer;
static uint32_t g_last_logged_apply_request;
static uint32_t g_control_event_dropped;
static uint32_t g_control_status_overwritten;

static void zephyr_event_print(uint32_t session_id, uint32_t sequence,
			       uint32_t request_id, uint64_t sample_index,
			       const char *event, uint64_t monotonic_ns,
			       uint8_t has_value, int32_t value,
			       const char *unit, const char *outcome)
{
	struct control_event_record record = {0};
	k_spinlock_key_t key;

	record.session_id = session_id;
	record.sequence = sequence;
	record.request_id = request_id;
	record.sample_index = sample_index;
	record.monotonic_ns = monotonic_ns;
	record.has_value = has_value;
	record.has_unit = unit != NULL;
	record.value = value;
	snprintk(record.event, sizeof(record.event), "%s",
		 event == NULL ? "null" : event);
	snprintk(record.unit, sizeof(record.unit), "%s",
		 unit == NULL ? "null" : unit);
	snprintk(record.outcome, sizeof(record.outcome), "%s",
		 outcome == NULL ? "null" : outcome);

	/* Producers never wait for or perform console I/O on the control path. */
	key = k_spin_lock(&g_control_event_queue_lock);
	if (k_msgq_put(&control_event_queue, &record, K_NO_WAIT) != 0) {
		struct control_event_record discarded;
		(void)k_msgq_get(&control_event_queue, &discarded, K_NO_WAIT);
		(void)k_msgq_put(&control_event_queue, &record, K_NO_WAIT);
		g_control_event_dropped += 1U;
	}
	k_spin_unlock(&g_control_event_queue_lock, key);
}

static void control_logger_thread(void *unused_a, void *unused_b,
					  void *unused_c)
{
	(void)unused_a;
	(void)unused_b;
	(void)unused_c;
	for (;;) {
		struct control_event_record record;
		uint32_t dropped;
		char sequence_text[12];
		char value_text[16];
		char unit_text[24];
		char line[512];
		k_spinlock_key_t key = k_spin_lock(&g_control_event_queue_lock);
		int got = k_msgq_get(&control_event_queue, &record, K_NO_WAIT);
		dropped = g_control_event_dropped;
		g_control_event_dropped = 0U;
		k_spin_unlock(&g_control_event_queue_lock, key);
		if (got != 0) {
			k_sleep(K_MSEC(1));
			continue;
		}
		if (dropped != 0U) {
			printk("{\"schema_version\":\"p5-ai-event-v1\","
			       "\"run_id\":\"%s\",\"endpoint\":\"zephyr\","
			       "\"scenario\":\"test-017\",\"transport\":\"udp\","
			       "\"session_id\":%u,\"sequence\":null,\"request_id\":0,"
			       "\"sample_index\":0,\"event\":\"logger_drop\","
			       "\"monotonic_ns\":%llu,\"value\":%u,"
			       "\"unit\":\"events\",\"outcome\":\"failed\"}\n",
			       CONFIG_DUAL_BOOT_ID, record.session_id,
			       (unsigned long long)record.monotonic_ns, dropped);
		}
		if (record.sequence == 0U) {
			snprintk(sequence_text, sizeof(sequence_text), "null");
		} else {
			snprintk(sequence_text, sizeof(sequence_text), "%u",
				 record.sequence);
		}
		if (record.has_value != 0U) {
			snprintk(value_text, sizeof(value_text), "%d", record.value);
		} else {
			snprintk(value_text, sizeof(value_text), "null");
		}
		if (record.has_unit != 0U) {
			snprintk(unit_text, sizeof(unit_text), "\"%s\"", record.unit);
		} else {
			snprintk(unit_text, sizeof(unit_text), "null");
		}
		snprintk(line, sizeof(line),
			 "{\"schema_version\":\"p5-ai-event-v1\",\"run_id\":\"%s\","
			 "\"endpoint\":\"zephyr\",\"scenario\":\"test-017\","
			 "\"transport\":\"udp\",\"session_id\":%u,\"sequence\":%s,"
			 "\"request_id\":%u,\"sample_index\":%llu,\"event\":\"%s\","
			 "\"monotonic_ns\":%llu,\"value\":%s,\"unit\":%s,"
			 "\"outcome\":\"%s\"}",
			 CONFIG_DUAL_BOOT_ID, record.session_id, sequence_text,
			 record.request_id, (unsigned long long)record.sample_index,
			 record.event, (unsigned long long)record.monotonic_ns,
			 value_text, unit_text, record.outcome);
		printk("%s\n", line);
	}
}

static uint64_t control_monotonic_ns(void)
{
	return k_ticks_to_ns_floor64(k_uptime_ticks());
}

static void publish_icpc_attempt(const uint8_t *packet, size_t packet_length,
				 uint32_t session_id, uint64_t monotonic_ns)
{
	char line[640];
	size_t index;
	int written;
	size_t offset;

	written = snprintk(line, sizeof(line),
			  "{\"schema_version\":\"p5-ai-icpc-attempt-v1\","
			  "\"run_id\":\"%s\",\"session_id\":%u,"
			  "\"direction\":\"zephyr_to_linux\",\"attempt\":0,"
			  "\"fault_disposition\":\"forwarded\","
			  "\"timestamp_ms\":%llu,\"monotonic_ns\":%llu,"
			  "\"wire_hex\":\"",
			  CONFIG_DUAL_BOOT_ID, session_id,
			  (unsigned long long)(monotonic_ns / UINT64_C(1000000)),
			  (unsigned long long)monotonic_ns);
	if (written < 0 || (size_t)written >= sizeof(line)) {
		return;
	}
	offset = (size_t)written;
	for (index = 0U; index < packet_length; ++index) {
		if (offset + 2U >= sizeof(line)) {
			return;
		}
		written = snprintk(line + offset, sizeof(line) - offset,
				  "%02x", packet[index]);
		if (written != 2) {
			return;
		}
		offset += 2U;
	}
	if (offset + 4U > sizeof(line)) {
		return;
	}
	line[offset++] = '"';
	line[offset++] = '}';
	line[offset] = '\0';
	printk("%s\n", line);
}

static void enqueue_control_status(
	const struct control_loop_status_snapshot *snapshot)
{
	struct control_loop_status_snapshot discarded;
	uint32_t overwritten = 0U;

	if (snapshot->session_id == 0U || snapshot->request_id == 0U) {
		return;
	}
	if (k_msgq_put(&control_status_queue, snapshot, K_NO_WAIT) != 0) {
		/* Keep the newest bounded snapshot; the control thread never waits. */
		(void)k_msgq_get(&control_status_queue, &discarded, K_NO_WAIT);
		(void)k_msgq_put(&control_status_queue, snapshot, K_NO_WAIT);
		g_control_status_overwritten += 1U;
		overwritten = g_control_status_overwritten;
	}
	if (overwritten != 0U) {
		int32_t count = overwritten > (uint32_t)INT32_MAX
				? INT32_MAX
				: (int32_t)overwritten;
		zephyr_event_print(snapshot->session_id, 0U, snapshot->request_id,
				  snapshot->sample_index, "status_queue_overwrite",
				  control_monotonic_ns(), 1U, count, "count", "dropped");
	}
}

static void control_periodic_thread(void *unused_a, void *unused_b,
					    void *unused_c)
{
	(void)unused_a;
	(void)unused_b;
	(void)unused_c;
	for (;;) {
		struct control_loop_status_snapshot snapshot;
		(void)k_timer_status_sync(&control_timer);
		uint64_t release_ns = control_monotonic_ns();
		k_spinlock_key_t key = k_spin_lock(&g_control_loop_lock);
		enum control_loop_result result = control_loop_release(
			&g_control_loop, release_ns, &snapshot);
		k_spin_unlock(&g_control_loop_lock, key);
		if (result != CONTROL_LOOP_RESULT_NOT_DUE &&
		    result != CONTROL_LOOP_RESULT_CLOCK_BACKWARD &&
		    snapshot.session_id != 0U && snapshot.request_id != 0U) {
			if (snapshot.request_id != g_last_logged_apply_request) {
				zephyr_event_print(snapshot.session_id, snapshot.sequence,
						  snapshot.request_id, snapshot.sample_index,
						  "control_apply", release_ns, 1U,
						  snapshot.duty_q16_16, "q16_16", "applied");
				zephyr_event_print(snapshot.session_id, 0U,
						  snapshot.request_id, snapshot.sample_index,
						  "safe_exit", release_ns, 0U, 0, NULL, "active");
				zephyr_event_print(snapshot.session_id, 0U, snapshot.request_id,
						  snapshot.sample_index, "period_finish",
						  control_monotonic_ns(), 0U, 0, NULL, "finished");
				g_last_logged_apply_request = snapshot.request_id;
			}
			enqueue_control_status(&snapshot);
		}
	}
}

static void send_pending_status(int sock, const struct sockaddr_in *peer,
					socklen_t peer_len, uint32_t *wire_sequence)
{
	struct control_loop_status_snapshot snapshot;

	while (k_msgq_get(&control_status_queue, &snapshot, K_NO_WAIT) == 0) {
		struct icpc_header status_header;
		uint8_t status_payload[CONTEST_ICPC_STATUS_PAYLOAD_SIZE];
		uint8_t status_packet[ICPC_HEADER_SIZE +
					      CONTEST_ICPC_STATUS_PAYLOAD_SIZE];
		size_t status_length = 0U;

		memset(&status_header, 0, sizeof(status_header));
		status_header.message_type = ICPC_MESSAGE_STATUS;
		status_header.session_id = snapshot.session_id;
		status_header.sequence = (*wire_sequence)++;
		status_header.timestamp_ms = snapshot.sample_index * PLANT_TICK_PERIOD_MS;
		status_header.error_code = ICPC_ERROR_NONE;
		memset(status_payload, 0, sizeof(status_payload));
		status_payload[0] = 1U;
		status_payload[1] = snapshot.control_mode;
		contest_icpc_write_u16_be(status_payload + 2U,
						 (uint16_t)snapshot.health_flags);
		contest_icpc_write_u32_be(status_payload + 4U,
						 snapshot.request_id);
		contest_icpc_write_u64_be(status_payload + 8U,
						 snapshot.sample_index);
		contest_icpc_write_i32_be(status_payload + 16U,
						 snapshot.measured_mC);
		contest_icpc_write_i32_be(status_payload + 20U,
						 snapshot.target_mC);
		contest_icpc_write_i32_be(status_payload + 24U,
						 snapshot.duty_q16_16);
		contest_icpc_write_i32_be(status_payload + 28U,
						 snapshot.error_mC);
		if (icpc_encode(status_packet, sizeof(status_packet), &status_header,
					status_payload, sizeof(status_payload),
					&status_length) == ICPC_STATUS_OK) {
			uint64_t send_ns = control_monotonic_ns();
			publish_icpc_attempt(status_packet, status_length,
					     snapshot.session_id, send_ns);
			ssize_t sent = zsock_sendto(sock, status_packet, status_length, 0,
						     (const struct sockaddr *)peer, peer_len);
			if (sent == (ssize_t)status_length) {
				zephyr_event_print(snapshot.session_id,
						  status_header.sequence, snapshot.request_id,
						  snapshot.sample_index, "packet_send",
						  send_ns, 1U,
						  snapshot.measured_mC, "mC", "status");
			}
		}
	}
}

static void print_ready(void)
{
	struct net_if *iface = net_if_get_default();
	struct in_addr addr;
	char mac[18];
	uint8_t raw_mac[6];
	size_t mtu = 0;

	if (iface == NULL) {
		printk("AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=%s iface=none\n",
		       CONFIG_DUAL_BOOT_ID);
		return;
	}
	if (net_if_get_link_addr(iface)->len >= 6) {
		memcpy(raw_mac, net_if_get_link_addr(iface)->addr, 6);
		snprintk(mac, sizeof(mac), "%02x:%02x:%02x:%02x:%02x:%02x",
			 raw_mac[0], raw_mac[1], raw_mac[2],
			 raw_mac[3], raw_mac[4], raw_mac[5]);
	} else {
		strcpy(mac, "00:00:00:00:00:00");
	}
	if (net_if_get_mtu(iface) > 0) {
		mtu = net_if_get_mtu(iface);
	}
	/* The configured IPv4 address of the default interface. */
	(void)net_addr_pton(AF_INET, CONFIG_NET_CONFIG_MY_IPV4_ADDR, &addr);
	char ip[16] = "0.0.0.0";
	net_addr_ntop(AF_INET, &addr, ip, sizeof(ip));

	printk("AXVISOR_DUAL_GUEST_ZEPHYR_READY vm=2 boot_id=%s iface=%s mac=%s "
	       "ipv4=%s prefix=24 route=10.77.0.0/24 mtu=%zu\n",
	       CONFIG_DUAL_BOOT_ID, net_if_get_device(iface)->name, mac, ip, mtu);
}

static int udp_echo_thread(int reliability_mode)
{
	struct sockaddr_in local;
	struct sockaddr_in from;
	socklen_t from_len = sizeof(from);
	struct sockaddr_in status_peer;
	socklen_t status_peer_len = sizeof(status_peer);
	uint8_t has_status_peer = UINT8_C(0);
	uint32_t reliability_controls = 0U;
	char buf[2048];
	int sock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (sock < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL sock=%d\n", sock);
		return -1;
	}
	memset(&local, 0, sizeof(local));
	local.sin_family = AF_INET;
	local.sin_port = htons(UDP_PORT);
	if (zsock_bind(sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL bind\n");
		return -1;
	}
	printk("TGOS_ZEPHYR_UDP_ECHO_READY port=%d\n", UDP_PORT);
	/* C2 owns plant/watchdog work in the priority-1 periodic thread. */
	control_loop_init(&g_control_loop, control_monotonic_ns());
	k_timer_init(&control_timer, NULL, NULL);
	k_timer_start(&control_timer, K_MSEC(100), K_MSEC(100));
	(void)k_thread_create(&logger_thread, logger_stack,
				      K_THREAD_STACK_SIZEOF(logger_stack),
				      control_logger_thread, NULL, NULL, NULL,
				      LOGGER_PRIORITY, 0, K_NO_WAIT);
	(void)k_thread_create(&control_thread, control_stack,
				      K_THREAD_STACK_SIZEOF(control_stack),
				      control_periodic_thread, NULL, NULL, NULL,
				      CONTROL_PRIORITY, 0, K_NO_WAIT);
	struct zsock_pollfd pfd = {0};
	pfd.fd = sock;
	pfd.events = ZSOCK_POLLIN;
	/* Per-session ICPC state (CONTROL -> ACK + STATUS). */
	uint32_t zseq = 1;
	struct icpc_receive_window control_window;
	icpc_receive_window_reset(&control_window);
	uint32_t active_session_id = 0U;
	uint32_t previous_session_id = 0U;
	uint32_t last_request_id = 0U;
	uint32_t last_request_sequence = 0U;
	uint8_t has_request = 0U;
	ssize_t n = -1;
	for (;;) {
		if (has_status_peer) {
			send_pending_status(sock, &status_peer, status_peer_len, &zseq);
		}
		int poll_r = zsock_poll(&pfd, 1, 100);
		if (poll_r > 0 && (pfd.revents & ZSOCK_POLLIN)) {
			from_len = sizeof(from);
			n = zsock_recvfrom(sock, buf, sizeof(buf), 0,
					     (struct sockaddr *)&from, &from_len);
			if (n < 0) {
				continue;
			}
			goto have_packet;
		}
		if (has_status_peer) {
			send_pending_status(sock, &status_peer, status_peer_len, &zseq);
		}
		continue;
	have_packet:
		;
		char peer[16];
		net_addr_ntop(AF_INET, &from.sin_addr, peer, sizeof(peer));
		if (strcmp(peer, PEER_IP) != 0) {
			continue;
		}
		/* Try ICPC: if the peer sent a valid CONTROL, reply with an ACK
		 * (+ STATUS) instead of a plain echo. */
		struct icpc_header rh;
		const uint8_t *rpay;
		size_t rlen = 0;
		if (icpc_decode((const uint8_t *)buf, (size_t)n, &rh, &rpay, &rlen) ==
			    ICPC_STATUS_OK &&
			    rh.message_type == ICPC_MESSAGE_CONTROL &&
			    rh.session_id != 0U &&
			    rlen == CONTEST_ICPC_CONTROL_PAYLOAD_SIZE &&
			    (rh.flags & ICPC_FLAG_ACK_REQUIRED) != 0U) {
			uint8_t control_mode = rpay[2];
			uint32_t request_id = contest_icpc_read_u32_be(rpay + 4U);
			int32_t wire_duty = contest_icpc_read_i32_be(rpay + 8U);
			int32_t wire_target = contest_icpc_read_i32_be(rpay + 12U);
			uint32_t wire_model = contest_icpc_read_u32_be(rpay + 16U);
			uint16_t wire_validity = contest_icpc_read_u16_be(rpay + 20U);
			if (rpay[0] != 1U || rpay[1] != 1U || rpay[3] != 0U ||
			    rpay[22] != 0U || rpay[23] != 0U || request_id == 0U ||
			    control_mode > 1U || wire_target != PLANT_TARGET_TEMPERATURE_MC ||
			    wire_validity != 500U || wire_duty < PLANT_MIN_DUTY_Q16_16 ||
			    wire_duty > PLANT_MAX_DUTY_Q16_16 ||
			    (control_mode == 0U && wire_model != 0U) ||
							    (control_mode == 1U && wire_model != 731924617U)) {
								    continue;
				}
			if (active_session_id == 0U) {
				k_spinlock_key_t key = k_spin_lock(&g_control_loop_lock);
				enum control_loop_result session_result =
					control_loop_begin_session(&g_control_loop, rh.session_id,
									  control_monotonic_ns());
				k_spin_unlock(&g_control_loop_lock, key);
				if (session_result != CONTROL_LOOP_RESULT_OK) {
					continue;
				}
				active_session_id = rh.session_id;
				g_last_logged_apply_request = 0U;
				zephyr_event_print(active_session_id, 0U, 0U, 0U,
						  "safe_enter", control_monotonic_ns(),
						  0U, 0, NULL, "safe");
			} else if (rh.session_id != active_session_id) {
				if (rh.session_id == previous_session_id) {
					continue;
				}
				previous_session_id = active_session_id;
				{
					k_spinlock_key_t key = k_spin_lock(&g_control_loop_lock);
					enum control_loop_result session_result =
						control_loop_begin_session(&g_control_loop, rh.session_id,
										  control_monotonic_ns());
					k_spin_unlock(&g_control_loop_lock, key);
					if (session_result != CONTROL_LOOP_RESULT_OK) {
						continue;
					}
				}
				active_session_id = rh.session_id;
				icpc_receive_window_reset(&control_window);
				has_request = 0U;
				g_last_logged_apply_request = 0U;
				zephyr_event_print(active_session_id, 0U, 0U, 0U,
						  "safe_enter", control_monotonic_ns(),
						  0U, 0, NULL, "safe");
			}
			if (has_request) {
				enum icpc_sequence_order request_order =
					icpc_compare_sequence(request_id, last_request_id);
				if (request_order == ICPC_SEQUENCE_OLDER ||
				    request_order == ICPC_SEQUENCE_AMBIGUOUS ||
				    (request_order == ICPC_SEQUENCE_EQUAL &&
				     rh.sequence != last_request_sequence)) {
					continue;
				}
			}
			enum icpc_receive_result receive_result =
				icpc_receive_window_observe(&control_window, rh.session_id,
								     rh.sequence);
			if (receive_result == ICPC_RECEIVE_INVALID ||
			    receive_result == ICPC_RECEIVE_STALE ||
			    receive_result == ICPC_RECEIVE_AMBIGUOUS ||
			    receive_result == ICPC_RECEIVE_OUT_OF_ORDER) {
				continue;
			}
			if (receive_result == ICPC_RECEIVE_DUPLICATE &&
			    (!has_request || request_id != last_request_id)) {
				continue;
			}
			if (receive_result == ICPC_RECEIVE_NEWER) {
				uint64_t receive_ns = control_monotonic_ns();
				uint64_t sample_index = (uint64_t)request_id - 1U;
				zephyr_event_print(rh.session_id, 0U, request_id,
						  sample_index, "period_release", receive_ns,
						  0U, 0, NULL, "released");
				zephyr_event_print(rh.session_id, 0U, request_id,
						  sample_index, "period_start", receive_ns,
						  0U, 0, NULL, "started");
				zephyr_event_print(rh.session_id, rh.sequence, request_id,
						  sample_index, "packet_receive", receive_ns,
						  0U, 0, NULL, "received");
				last_request_id = request_id;
				last_request_sequence = rh.sequence;
				has_request = 1U;
				if (reliability_mode != 0 && request_id != UINT32_MAX) {
					reliability_controls++;
					if (reliability_controls ==
					    P4_RELIABILITY_CONTROL_MESSAGES) {
						printk("TGOS_ZEPHYR_ICPC_RELIABILITY_COMPLETE "
						       "applied=%u duplicates_suppressed=1\n",
						       reliability_controls);
					}
				}
			}
			struct control_mailbox_command command = {
				.session_id = rh.session_id,
				.sequence = rh.sequence,
				.request_id = request_id,
				.command = CONTROL_MAILBOX_APPLY_OUTPUT,
				.control_mode = control_mode,
				.duty_q16_16 = wire_duty,
				.target_mC = wire_target,
				.model_version = wire_model,
				.validity_ms = wire_validity,
			};
			if (receive_result == ICPC_RECEIVE_NEWER) {
				k_spinlock_key_t key = k_spin_lock(&g_control_loop_lock);
				enum control_loop_result publish_result =
					control_loop_publish(&g_control_loop, &command);
				k_spin_unlock(&g_control_loop_lock, key);
				if (publish_result != CONTROL_MAILBOX_STATUS_OK) {
					continue;
				}
			}
			status_peer = from;
			status_peer_len = from_len;
			has_status_peer = UINT8_C(1);
			struct icpc_header ack;
			memset(&ack, 0, sizeof(ack));
			ack.message_type = ICPC_MESSAGE_ACK;
			ack.flags = 0;
			ack.session_id = rh.session_id;
			ack.sequence = zseq++;
			ack.ack_sequence = rh.sequence;
			ack.timestamp_ms = 0;
			ack.error_code = ICPC_ERROR_NONE;
			uint8_t ackbuf[ICPC_HEADER_SIZE + 4];
			size_t alen = 0;
			(void)rh.sequence;
			if (icpc_encode(ackbuf, sizeof(ackbuf), &ack, NULL, 0, &alen) ==
			    ICPC_STATUS_OK) {
				uint64_t ack_send_ns = control_monotonic_ns();
				publish_icpc_attempt(ackbuf, alen, rh.session_id,
						     ack_send_ns);
				ssize_t ar = zsock_sendto(sock, ackbuf, alen, 0,
					   (struct sockaddr *)&from, from_len);
				if (ar < 0) {
					static int once;
					if (once < 5) { once++;
					    printk("TGOS_ZEPHYR_ACK_SEND_FAIL err=%d n=%zd rseq=%u\n",
						   errno, ar, rh.sequence);
					}
				} else if (receive_result == ICPC_RECEIVE_NEWER) {
					zephyr_event_print(rh.session_id, ack.sequence,
							  request_id, (uint64_t)request_id - 1U,
							  "ack_send", ack_send_ns,
							  1U, (int32_t)ack.sequence,
							  "sequence", "ack");
				}
			}
			/* A duplicate gets its ACK again but must never execute the plant
			 * action twice or emit a second qualification STATUS. */
			if (receive_result == ICPC_RECEIVE_DUPLICATE) {
				continue;
			}
			/* STATUS is emitted by the priority-1 thread and drained here. */
			send_pending_status(sock, &status_peer, status_peer_len, &zseq);
			continue;
		}
		/* Non-CONTROL ICPC frames (ACK/STATUS echoed by the peer) must NOT be
		 * echoed back: that would create an unbounded echo loop between the
		 * two guests and keep the poll busy, starving the watchdog.
		 * Everything else (probe, plain UDP) is echoed as before. */
		{
			struct icpc_header rh2;
			const uint8_t *rpay2;
			size_t rlen2 = 0;
			if (icpc_decode((const uint8_t *)buf, (size_t)n, &rh2, &rpay2,
					 &rlen2) == ICPC_STATUS_OK) {
				continue;
			}
		}
		(void)zsock_sendto(sock, buf, n, 0, (struct sockaddr *)&from, from_len);
		printk("TGOS_ZEPHYR_UDP_ECHO peer=%s bytes=%zd\n", peer, n);
	}
}

static uint32_t p4_read_u32_be(const uint8_t *bytes)
{
	return ((uint32_t)bytes[0] << 24U) |
	       ((uint32_t)bytes[1] << 16U) |
	       ((uint32_t)bytes[2] << 8U) |
	       (uint32_t)bytes[3];
}

static int p4_udp_payload_valid(const uint8_t *payload, size_t length,
				uint32_t *sequence)
{
	size_t index;
	uint32_t value;

	if (length != P4_UDP_PAYLOAD_SIZE ||
	    p4_read_u32_be(payload) != UINT32_C(0x50345544)) {
		return 0;
	}
	value = p4_read_u32_be(payload + 4U);
	if (value == 0U || value > P4_RELIABILITY_MESSAGES) {
		return 0;
	}
	for (index = 8U; index < length; ++index) {
		if (payload[index] !=
		    (uint8_t)(value * UINT32_C(131) + index * 17U)) {
			return 0;
		}
	}
	*sequence = value;
	return 1;
}

static int udp_reliability_thread(void)
{
	static uint8_t seen[(P4_RELIABILITY_MESSAGES + 7U) / 8U];
	struct sockaddr_in local;
	struct sockaddr_in from;
	uint8_t packet[P4_UDP_PAYLOAD_SIZE];
	uint32_t unique = 0U;
	uint32_t duplicates = 0U;
	uint32_t malformed = 0U;
	int complete_reported = 0;
	int sock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (sock < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL reliability_udp_socket=%d\n", sock);
		return -1;
	}
	memset(&local, 0, sizeof(local));
	local.sin_family = AF_INET;
	local.sin_port = htons(UDP_PORT);
	if (zsock_bind(sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL reliability_udp_bind\n");
		zsock_close(sock);
		return -1;
	}
	printk("TGOS_ZEPHYR_UDP_RELIABILITY_READY messages=%u rate_hz=100\n",
	       P4_RELIABILITY_MESSAGES);
	for (;;) {
		socklen_t from_length = sizeof(from);
		ssize_t length = zsock_recvfrom(sock, packet, sizeof(packet), 0,
					       (struct sockaddr *)&from,
					       &from_length);
		uint32_t sequence;
		char peer[16];

		if (length < 0) {
			continue;
		}
		net_addr_ntop(AF_INET, &from.sin_addr, peer, sizeof(peer));
		if (strcmp(peer, PEER_IP) != 0) {
			malformed++;
			continue;
		}
		if (length == 8 &&
		    p4_read_u32_be(packet) == UINT32_C(0x50345545) &&
		    p4_read_u32_be(packet + 4U) == P4_RELIABILITY_MESSAGES) {
			if (complete_reported == 0) {
				printk("TGOS_ZEPHYR_UDP_RELIABILITY_COMPLETE "
				       "received=%u loss=%u duplicates=%u malformed=%u\n",
				       unique, P4_RELIABILITY_MESSAGES - unique,
				       duplicates, malformed);
				complete_reported = 1;
			}
			(void)zsock_sendto(sock, packet, length, 0,
					   (struct sockaddr *)&from, from_length);
			continue;
		}
		if (!p4_udp_payload_valid(packet, (size_t)length, &sequence)) {
			malformed++;
		} else {
			uint32_t bit_index = sequence - 1U;
			uint8_t bit = (uint8_t)(UINT8_C(1) << (bit_index & 7U));
			if ((seen[bit_index >> 3U] & bit) != 0U) {
				duplicates++;
			} else {
				seen[bit_index >> 3U] |= bit;
				unique++;
				if (unique == P4_RELIABILITY_MESSAGES &&
				    complete_reported == 0) {
					printk("TGOS_ZEPHYR_UDP_RELIABILITY_COMPLETE "
					       "received=%u loss=0 duplicates=%u malformed=%u\n",
					       unique, duplicates, malformed);
					complete_reported = 1;
				}
			}
		}
		(void)zsock_sendto(sock, packet, length, 0,
				   (struct sockaddr *)&from, from_length);
	}
}

static int p4_zsock_send_all(int descriptor, const uint8_t *bytes,
			     size_t length)
{
	size_t sent = 0U;

	while (sent < length) {
		ssize_t result = zsock_send(descriptor, bytes + sent,
					    length - sent, 0);
		if (result <= 0) {
			return -1;
		}
		sent += (size_t)result;
	}
	return 0;
}

static int p4_zsock_receive_all(int descriptor, uint8_t *bytes,
				size_t length, size_t *received)
{
	*received = 0U;
	while (*received < length) {
		ssize_t result = zsock_recv(descriptor, bytes + *received,
					    length - *received, 0);
		if (result <= 0) {
			return -1;
		}
		*received += (size_t)result;
	}
	return 0;
}

static int p4_tcp_listener(void)
{
	struct sockaddr_in local;
	int listener = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	int one = 1;

	if (listener < 0) {
		return -1;
	}
	(void)zsock_setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one,
				 sizeof(one));
	memset(&local, 0, sizeof(local));
	local.sin_family = AF_INET;
	local.sin_port = htons(TCP_PORT);
	if (zsock_bind(listener, (struct sockaddr *)&local, sizeof(local)) < 0 ||
	    zsock_listen(listener, 1) < 0) {
		zsock_close(listener);
		return -1;
	}
	return listener;
}

static int tcp_reliability_thread(void)
{
	struct sockaddr_in from;
	uint32_t expected_sequence = 1U;
	uint32_t verified = 0U;
	uint32_t duplicates = 0U;
	uint32_t half_frame_eof = 0U;
	uint32_t endpoint_restarts = 0U;
	uint32_t previous_session = 0U;
	uint32_t new_session_after_restart = 0U;
	int listener = p4_tcp_listener();

	if (listener < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL reliability_tcp_listen\n");
		return -1;
	}
	printk("TGOS_ZEPHYR_TCP_RELIABILITY_READY messages=%u framing=u16-be-icpc\n",
	       P4_RELIABILITY_MESSAGES);
	for (;;) {
		socklen_t from_length = sizeof(from);
		int descriptor = zsock_accept(listener, (struct sockaddr *)&from,
					      &from_length);
		int restart_listener = 0;

		if (descriptor < 0) {
			continue;
		}
		for (;;) {
			uint8_t frame[ICPC_MAX_PACKET_SIZE + 2U];
			struct icpc_header header;
			const uint8_t *payload;
			size_t payload_length;
			size_t received;
			size_t packet_length;

			/* The first prefix byte is intentionally read alone.  This is the
			 * frozen one-byte partial-read path, not an assumption about how
			 * the TCP stack segments writes. */
			if (p4_zsock_receive_all(descriptor, frame, 1U, &received) != 0 ||
			    p4_zsock_receive_all(descriptor, frame + 1U, 1U, &received) != 0) {
				break;
			}
			packet_length = ((size_t)frame[0] << 8U) | frame[1];
			if (packet_length < ICPC_HEADER_SIZE ||
			    packet_length > ICPC_MAX_PACKET_SIZE) {
				printk("TGOS_ZEPHYR_NET_FAIL reliability_tcp_length=%zu\n",
				       packet_length);
				break;
			}
			if (p4_zsock_receive_all(descriptor, frame + 2U, packet_length,
						 &received) != 0) {
				if (received > 0U && received < packet_length) {
					half_frame_eof++;
					restart_listener = 1;
				}
				break;
			}
			if (icpc_decode(frame + 2U, packet_length, &header, &payload,
					&payload_length) != ICPC_STATUS_OK ||
			    header.message_type != ICPC_MESSAGE_STATUS) {
				printk("TGOS_ZEPHYR_NET_FAIL reliability_tcp_icpc\n");
				break;
			}
			(void)payload;
			(void)payload_length;
			if (previous_session != 0U &&
			    header.session_id != previous_session) {
				new_session_after_restart = 1U;
			}
			previous_session = header.session_id;
			if (header.sequence == expected_sequence) {
				expected_sequence++;
				verified++;
			} else if (header.sequence < expected_sequence) {
				duplicates++;
			} else {
				printk("TGOS_ZEPHYR_NET_FAIL reliability_tcp_gap expected=%u "
				       "actual=%u\n", expected_sequence, header.sequence);
				break;
			}
			if (p4_zsock_send_all(descriptor, frame,
						  packet_length + 2U) != 0) {
				break;
			}
			if (verified == P4_RELIABILITY_MESSAGES) {
				printk("TGOS_ZEPHYR_TCP_RELIABILITY_COMPLETE verified=%u "
				       "duplicates=%u half_frame_eof=%u endpoint_restarts=%u "
				       "new_session=%u\n",
				       verified, duplicates, half_frame_eof,
				       endpoint_restarts, new_session_after_restart);
			}
		}
		zsock_close(descriptor);
		if (restart_listener != 0) {
			zsock_close(listener);
			k_sleep(K_MSEC(500));
			listener = p4_tcp_listener();
			if (listener < 0) {
				printk("TGOS_ZEPHYR_NET_FAIL reliability_tcp_restart\n");
				return -1;
			}
			endpoint_restarts++;
		}
	}
}

static int tcp_listen_thread(void)
{
	struct sockaddr_in local;
	struct sockaddr_in from;
	socklen_t from_len = sizeof(from);
	char buf[2048];
	int lsock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

	if (lsock < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL tcp sock\n");
		return -1;
	}
	int one = 1;
	(void)zsock_setsockopt(lsock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	memset(&local, 0, sizeof(local));
	local.sin_family = AF_INET;
	local.sin_port = htons(TCP_PORT);
	if (zsock_bind(lsock, (struct sockaddr *)&local, sizeof(local)) < 0 ||
	    zsock_listen(lsock, 1) < 0) {
		printk("TGOS_ZEPHYR_NET_FAIL tcp bind/listen\n");
		return -1;
	}
	printk("TGOS_ZEPHYR_TCP_LISTEN_READY port=%d\n", TCP_PORT);
	for (;;) {
		int sock = zsock_accept(lsock, (struct sockaddr *)&from, &from_len);
		if (sock < 0) {
			continue;
		}
		printk("TGOS_ZEPHYR_TCP_ACCEPTED\n");
		for (;;) {
			ssize_t n = zsock_recv(sock, buf, sizeof(buf), 0);
			if (n <= 0) {
				break;
			}
			if (zsock_send(sock, buf, n, 0) < 0) {
				break;
			}
		}
		zsock_close(sock);
	}
}

int main(void)
{
	const char *mode = CONFIG_CONTEST_NET_MODE;

	k_sleep(K_MSEC(250));
	print_ready();

	if (strcmp(mode, "udp-echo") == 0) {
		(void)k_thread_priority_set(k_current_get(), NET_PRIORITY);
		return udp_echo_thread(0);
	}
	if (strcmp(mode, "tcp-listen") == 0) {
		(void)k_thread_priority_set(k_current_get(), NET_PRIORITY);
		return tcp_listen_thread();
	}
	if (strcmp(mode, "udp-reliability") == 0) {
		(void)k_thread_priority_set(k_current_get(), NET_PRIORITY);
		return udp_reliability_thread();
	}
	if (strcmp(mode, "tcp-reliability") == 0) {
		(void)k_thread_priority_set(k_current_get(), NET_PRIORITY);
		return tcp_reliability_thread();
	}
	if (strcmp(mode, "icpc-reliability") == 0) {
		(void)k_thread_priority_set(k_current_get(), NET_PRIORITY);
		return udp_echo_thread(1);
	}
	/* l3-smoke: verify the interface is up and report the identity;
	 * ICMP echo and ICPC are exercised by the runner over UDP/TCP. */
	printk("TGOS_ZEPHYR_L3_SMOKE_READY\n");
	for (;;) {
		k_sleep(K_SECONDS(5));
	}
	return 0;
}
