"""Shared host-side runtime contracts for contest dual-Guest runners.

The package intentionally contains no AxVisor or QEMU dependency at import
time.  Static contract tests can therefore exercise the identity, console and
publication rules on Windows; the runner imports the process/QMP adapters only
when an explicitly requested live run reaches that stage.
"""

from .dual_guest import (
    DualGuestSpec,
    RuntimeContractError,
    build_axvisor_qemu_command,
    inject_qemu_identity_args,
    parse_dtb_evidence,
    request_bounded_shutdown,
    validate_dual_guest_log,
    validate_no_data_plane_qemu_config,
    validate_no_data_plane_vm_config,
)

__all__ = [
    "DualGuestSpec",
    "RuntimeContractError",
    "build_axvisor_qemu_command",
    "inject_qemu_identity_args",
    "parse_dtb_evidence",
    "request_bounded_shutdown",
    "validate_dual_guest_log",
    "validate_no_data_plane_qemu_config",
    "validate_no_data_plane_vm_config",
]
