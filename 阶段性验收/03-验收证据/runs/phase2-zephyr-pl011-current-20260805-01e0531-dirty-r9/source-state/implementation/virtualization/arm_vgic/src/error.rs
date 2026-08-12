//! Typed errors reported by the virtual Generic Interrupt Controller.

use alloc::string::String;

use axdevice_base::{AccessWidth, DeviceError};

/// Result type returned by VGIC operations.
pub type VgicResult<T = ()> = Result<T, VgicError>;

/// Errors reported by the virtual Generic Interrupt Controller.
#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
pub enum VgicError {
    /// An IRQ identifier is outside the supported range.
    #[error("VGIC IRQ {irq} is outside the supported range 0..{max}")]
    InvalidIrq {
        /// The rejected IRQ identifier.
        irq: usize,
        /// The exclusive upper bound for valid IRQ identifiers.
        max: usize,
    },
    /// An IRQ identifier is valid in the GIC but is not an assignable SPI.
    #[error("VGIC IRQ {irq} is not an SPI in the supported range {first}..{end}")]
    InvalidSpi {
        /// The rejected IRQ identifier.
        irq: usize,
        /// The first assignable SPI identifier.
        first: usize,
        /// The exclusive upper bound for assignable SPIs.
        end: usize,
    },
    /// The same VGIC distributor already owns the requested SPI.
    #[error("VGIC IRQ {irq} is already assigned")]
    IrqAlreadyAssigned {
        /// The duplicate IRQ identifier.
        irq: usize,
    },
    /// A register access has an invalid address or width.
    #[error("invalid VGIC {operation} at offset {offset:#x} with width {width:?}")]
    InvalidAccess {
        /// Whether the access is a read or write.
        operation: &'static str,
        /// Register offset from the controller base.
        offset: usize,
        /// Width of the register access.
        width: AccessWidth,
    },
    /// A register or controller operation is unsupported.
    #[error("unsupported VGIC operation {operation}: {detail}")]
    Unsupported {
        /// The unsupported operation.
        operation: &'static str,
        /// Diagnostic detail describing the limitation.
        detail: String,
    },
    /// A host GIC or MMIO backend operation failed.
    #[error("VGIC backend operation {operation} failed: {detail}")]
    Backend {
        /// The backend operation that failed.
        operation: &'static str,
        /// Diagnostic detail from the backend.
        detail: String,
    },
}

impl From<VgicError> for DeviceError {
    fn from(error: VgicError) -> Self {
        match error {
            VgicError::InvalidIrq { .. }
            | VgicError::InvalidSpi { .. }
            | VgicError::InvalidAccess { .. } => Self::InvalidInput {
                operation: "access ARM VGIC",
                detail: alloc::format!("{error}"),
            },
            VgicError::IrqAlreadyAssigned { irq } => Self::ResourceBusy {
                operation: "assign ARM VGIC IRQ",
                resource: alloc::format!("IRQ {irq}"),
            },
            VgicError::Unsupported { .. } => Self::Unsupported {
                operation: "access ARM VGIC",
                detail: alloc::format!("{error}"),
            },
            VgicError::Backend { .. } => Self::Backend {
                operation: "access ARM VGIC",
                detail: alloc::format!("{error}"),
            },
        }
    }
}
