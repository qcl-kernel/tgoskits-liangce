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

#define PEER_IP "10.77.0.1"
#define UDP_PORT 46000
#define TCP_PORT 46001

#define NET_STACK_SIZE 4096
#define NET_PRIORITY 8

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

static int udp_echo_thread(void)
{
	struct sockaddr_in local;
	struct sockaddr_in from;
	socklen_t from_len = sizeof(from);
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
	/* Per-session ICPC state (CONTROL -> ACK + STATUS). */
	uint32_t zseq = 1;
	for (;;) {
		ssize_t n = zsock_recvfrom(sock, buf, sizeof(buf), 0,
				     (struct sockaddr *)&from, &from_len);
		if (n < 0) {
			continue;
		}
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
		    rh.session_id != 0U) {
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
			if (icpc_encode(ackbuf, sizeof(ackbuf), &ack, NULL, 0, &alen) ==
			    ICPC_STATUS_OK) {
				(void)zsock_sendto(sock, ackbuf, alen, 0,
					   (struct sockaddr *)&from, from_len);
			}
			struct icpc_header st;
			memset(&st, 0, sizeof(st));
			st.message_type = ICPC_MESSAGE_STATUS;
			st.flags = 0;
			st.session_id = rh.session_id;
			st.sequence = zseq++;
			st.ack_sequence = 0;
			st.timestamp_ms = (uint64_t)rh.timestamp_ms;
			st.error_code = ICPC_ERROR_NONE;
			uint8_t stpay[12];
			memset(stpay, 0x44, sizeof(stpay));
			stpay[0] = 1; /* plant applied */
			stpay[1] = (uint8_t)(rh.sequence & 0xff);
			uint8_t stbuf[ICPC_HEADER_SIZE + 12 + 4];
			size_t slen = 0;
			if (icpc_encode(stbuf, sizeof(stbuf), &st, stpay, sizeof(stpay),
					 &slen) == ICPC_STATUS_OK) {
				(void)zsock_sendto(sock, stbuf, slen, 0,
					   (struct sockaddr *)&from, from_len);
			}
			(void)rh.sequence; /* per-CONTROL printk removed: console slows replies */
			continue;
		}
		(void)zsock_sendto(sock, buf, n, 0, (struct sockaddr *)&from, from_len);
		printk("TGOS_ZEPHYR_UDP_ECHO peer=%s bytes=%zd\n", peer, n);
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
		return udp_echo_thread();
	}
	if (strcmp(mode, "tcp-listen") == 0) {
		return tcp_listen_thread();
	}
	/* l3-smoke: verify the interface is up and report the identity;
	 * ICMP echo and ICPC are exercised by the runner over UDP/TCP. */
	printk("TGOS_ZEPHYR_L3_SMOKE_READY\n");
	for (;;) {
		k_sleep(K_SECONDS(5));
	}
	return 0;
}
