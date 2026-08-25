"""Pure host-side references for the frozen P5 control contract.

This module deliberately has no socket, Guest, Zephyr, or model-runtime
dependency.  It is a deterministic reference for host tests and later data
generation; it is not an AI-loop or Guest runtime implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


INITIAL_TEMPERATURE_MC = 25_000
AMBIENT_TEMPERATURE_MC = 25_000
TARGET_TEMPERATURE_MC = 55_000
SAFE_DUTY_Q16_16 = 0
TICK_PERIOD_MS = 100
HEATER_RATE_MC_PER_SECOND = 4_000
HEAT_LOSS_DIVISOR = 100
MIN_TEMPERATURE_MC = -40_000
MAX_TEMPERATURE_MC = 125_000
DISTURBANCE_START_TICK = 600
DISTURBANCE_END_TICK = 900
DISTURBANCE_MC_PER_TICK = -150

MIN_DUTY_Q16_16 = 0
MAX_DUTY_Q16_16 = 65_536

PCG_MULTIPLIER = 6_364_136_223_846_793_005
PCG_INCREMENT = 1_442_695_040_888_963_407
UINT64_MASK = (1 << 64) - 1
UINT32_MASK = (1 << 32) - 1


class PlantOverflowError(OverflowError):
    """Raised when the integer plant would leave its representable range."""


def truncate_division_toward_zero(numerator: int, denominator: int) -> int:
    """Return C99-style integer division for a positive denominator."""

    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator < 0:
        return -((-numerator) // denominator)
    return numerator // denominator


def quantize_duty(duty: float) -> int:
    """Clamp a non-negative duty and round it to Q16.16."""

    if not math.isfinite(duty):
        raise ValueError("duty must be finite")
    clamped = min(max(duty, 0.0), 1.0)
    return min(MAX_DUTY_Q16_16, max(MIN_DUTY_Q16_16, math.floor(clamped * 65_536.0 + 0.5)))


def plant_step(temperature_mC: int, duty_q16_16: int, tick_index: int) -> int:
    """Advance the qualification plant by one 100 ms tick."""

    return plant_step_with_disturbance(
        temperature_mC,
        duty_q16_16,
        tick_index,
        DISTURBANCE_START_TICK,
        DISTURBANCE_END_TICK - DISTURBANCE_START_TICK,
        DISTURBANCE_MC_PER_TICK,
    )


def plant_step_with_disturbance(
    temperature_mC: int,
    duty_q16_16: int,
    tick_index: int,
    disturbance_start_tick: int,
    disturbance_duration_ticks: int,
    disturbance_mC_per_tick: int,
) -> int:
    """Advance the plant with an explicit deterministic disturbance profile."""

    if not MIN_DUTY_Q16_16 <= duty_q16_16 <= MAX_DUTY_Q16_16:
        raise ValueError("duty_q16_16 is outside 0..65536")
    if tick_index < 0:
        raise ValueError("tick_index must be non-negative")
    if disturbance_start_tick < 0 or disturbance_duration_ticks < 0:
        raise ValueError("disturbance bounds must be non-negative")

    loss_mC = truncate_division_toward_zero(
        AMBIENT_TEMPERATURE_MC - temperature_mC, HEAT_LOSS_DIVISOR
    )
    heater_mC = (HEATER_RATE_MC_PER_SECOND // 10 * duty_q16_16 + 32_768) // 65_536
    disturbance_end_tick = disturbance_start_tick + disturbance_duration_ticks
    disturbance_mC = (
        disturbance_mC_per_tick
        if disturbance_start_tick <= tick_index < disturbance_end_tick
        else 0
    )
    next_temperature_mC = temperature_mC + loss_mC + heater_mC + disturbance_mC
    if not MIN_TEMPERATURE_MC <= next_temperature_mC <= MAX_TEMPERATURE_MC:
        raise PlantOverflowError(
            f"plant result {next_temperature_mC} mC is outside "
            f"[{MIN_TEMPERATURE_MC}, {MAX_TEMPERATURE_MC}]"
        )
    return next_temperature_mC


def simulate_plant(
    duties_q16_16: Iterable[int], initial_temperature_mC: int = INITIAL_TEMPERATURE_MC
) -> list[int]:
    """Return one updated temperature for each supplied duty."""

    temperature_mC = initial_temperature_mC
    temperatures: list[int] = []
    for tick_index, duty_q16_16 in enumerate(duties_q16_16):
        temperature_mC = plant_step(temperature_mC, duty_q16_16, tick_index)
        temperatures.append(temperature_mC)
    return temperatures


@dataclass
class FixedPiController:
    """The frozen fixed-parameter controller used as the P5 baseline."""

    integral_c_seconds: float = 0.0
    target_mC: int | None = None

    def reset(self) -> None:
        """Clear the integral after SAFE, session, target, or mode changes."""

        self.integral_c_seconds = 0.0
        self.target_mC = None

    def update(self, measured_mC: int, target_mC: int = TARGET_TEMPERATURE_MC) -> int:
        """Compute one Q16.16 output using the specified anti-windup rule."""

        if self.target_mC is None or self.target_mC != target_mC:
            self.integral_c_seconds = 0.0
            self.target_mC = target_mC
        error_c = (target_mC - measured_mC) / 1_000.0
        previous_integral = self.integral_c_seconds
        candidate_integral = min(
            100.0, max(-100.0, previous_integral + 0.1 * error_c)
        )
        candidate_output = 0.025 * error_c + 0.005 * candidate_integral
        if (candidate_output > 1.0 and error_c > 0.0) or (
            candidate_output < 0.0 and error_c < 0.0
        ):
            self.integral_c_seconds = previous_integral
            output = 0.025 * error_c + 0.005 * previous_integral
        else:
            self.integral_c_seconds = candidate_integral
            output = candidate_output
        return quantize_duty(output)


@dataclass(frozen=True)
class EpisodeParameters:
    """The deterministic disturbance parameters for one training episode."""

    initial_temperature_mC: int
    target_mC: int
    disturbance_start_tick: int
    disturbance_duration_ticks: int
    disturbance_mC_per_tick: int


class Pcg32:
    """The PCG-XSH-RR 64/32 generator frozen by IF-008."""

    def __init__(self, seed: int):
        self.state = seed & UINT64_MASK

    def next_u32(self) -> int:
        old_state = self.state
        self.state = (old_state * PCG_MULTIPLIER + PCG_INCREMENT) & UINT64_MASK
        xorshifted = (((old_state >> 18) ^ old_state) >> 27) & UINT32_MASK
        rotation = (old_state >> 59) & 0x1F
        return ((xorshifted >> rotation) | (xorshifted << ((32 - rotation) & 31))) & UINT32_MASK


def episode_parameters(seed: int) -> EpisodeParameters:
    """Generate one canonical episode parameter tuple from a split seed."""

    generator = Pcg32(seed)
    generator.next_u32()
    return episode_parameters_from_generator(generator)


def episode_parameters_from_generator(generator: Pcg32) -> EpisodeParameters:
    """Consume four values from a canonical split generator."""

    r0, r1, r2, r3 = (generator.next_u32() for _ in range(4))
    return EpisodeParameters(
        initial_temperature_mC=25_000 + r0 % 15_001,
        target_mC=45_000 + r1 % 15_001,
        disturbance_start_tick=300 + r2 % 601,
        disturbance_duration_ticks=300,
        disturbance_mC_per_tick=-200 + r3 % 301,
    )


def generate_teacher_samples(
    parameters: EpisodeParameters, ticks: int = 1_800
) -> list[dict[str, int | list[int]]]:
    """Generate canonical PI-teacher samples for one episode."""

    if ticks <= 0:
        raise ValueError("ticks must be positive")
    temperature_mC = parameters.initial_temperature_mC
    applied_duty_q16_16 = SAFE_DUTY_Q16_16
    controller = FixedPiController()
    samples: list[dict[str, int | list[int]]] = []
    for tick_index in range(ticks):
        label_duty_q16_16 = controller.update(
            temperature_mC, parameters.target_mC
        )
        samples.append(
            {
                "tick": tick_index,
                "input": [
                    temperature_mC,
                    parameters.target_mC,
                    applied_duty_q16_16,
                ],
                "label": label_duty_q16_16,
            }
        )
        temperature_mC = plant_step_with_disturbance(
            temperature_mC,
            applied_duty_q16_16,
            tick_index,
            parameters.disturbance_start_tick,
            parameters.disturbance_duration_ticks,
            parameters.disturbance_mC_per_tick,
        )
        applied_duty_q16_16 = label_duty_q16_16
    return samples
