/* SPDX-License-Identifier: Apache-2.0 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#define PERIOD_MS 100
#define SAMPLE_COUNT 10

int main(void)
{
	int64_t previous = k_uptime_get();

	printk("TGOS_ZEPHYR_SMOKE_START period_ms=%d samples=%d\n", PERIOD_MS,
	       SAMPLE_COUNT);
	for (int sequence = 1; sequence <= SAMPLE_COUNT; ++sequence) {
		int64_t now;
		int64_t delta;

		k_msleep(PERIOD_MS);
		now = k_uptime_get();
		delta = now - previous;
		printk("TGOS_ZEPHYR_SMOKE_SAMPLE seq=%d uptime_ms=%lld delta_ms=%lld\n",
		       sequence, (long long)now, (long long)delta);
		previous = now;
	}

	printk("TGOS_ZEPHYR_SMOKE_PASS samples=10\n");
	return 0;
}
