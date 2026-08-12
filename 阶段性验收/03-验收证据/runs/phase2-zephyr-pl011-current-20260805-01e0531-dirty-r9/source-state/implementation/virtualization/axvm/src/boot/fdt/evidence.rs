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

//! Feature-gated final guest-DTB capture descriptors.

use alloc::{format, string::String, vec::Vec};

use ax_memory_addr::PAGE_SIZE_4K;

use crate::{AxVMRef, AxVmResult, GuestPhysAddr, HostPhysAddr, ax_err_type};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct HpaSegment {
    start: HostPhysAddr,
    length: usize,
}

impl HpaSegment {
    pub(crate) const fn new(start: HostPhysAddr, length: usize) -> Self {
        Self { start, length }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct GuestDtbEvidenceDescriptor {
    vm_id: usize,
    gpa: GuestPhysAddr,
    size: usize,
    hpa_segments: Vec<HpaSegment>,
}

impl GuestDtbEvidenceDescriptor {
    pub(crate) fn new(
        vm_id: usize,
        gpa: GuestPhysAddr,
        size: usize,
        hpa_segments: Vec<HpaSegment>,
    ) -> AxVmResult<Self> {
        if size == 0 || hpa_segments.is_empty() {
            return Err(ax_err_type!(
                InvalidData,
                "Guest DTB evidence requires non-empty bytes and HPA segments"
            ));
        }
        gpa.as_usize()
            .checked_add(size)
            .ok_or_else(|| ax_err_type!(InvalidData, "Guest DTB evidence GPA range overflows"))?;

        let mut covered = 0usize;
        for segment in &hpa_segments {
            if segment.length == 0 {
                return Err(ax_err_type!(
                    InvalidData,
                    "Guest DTB evidence contains an empty HPA segment"
                ));
            }
            segment
                .start
                .as_usize()
                .checked_add(segment.length)
                .ok_or_else(|| {
                    ax_err_type!(InvalidData, "Guest DTB evidence HPA segment overflows")
                })?;
            covered = covered.checked_add(segment.length).ok_or_else(|| {
                ax_err_type!(InvalidData, "Guest DTB evidence coverage length overflows")
            })?;
        }
        if covered != size {
            return Err(ax_err_type!(
                InvalidData,
                format!("Guest DTB evidence HPA coverage mismatch: expected {size}, got {covered}")
            ));
        }

        Ok(Self {
            vm_id,
            gpa,
            size,
            hpa_segments,
        })
    }

    pub(crate) fn marker(&self) -> String {
        let segments = self
            .hpa_segments
            .iter()
            .map(|segment| format!("{:#x}:{}", segment.start.as_usize(), segment.length))
            .collect::<Vec<_>>()
            .join(",");
        format!(
            "AXVISOR_GUEST_DTB_READY vm={} gpa={:#x} size={} hpa_segments={segments}",
            self.vm_id,
            self.gpa.as_usize(),
            self.size
        )
    }
}

pub(crate) fn resolve_hpa_segments(
    gpa: GuestPhysAddr,
    size: usize,
    mut translate: impl FnMut(GuestPhysAddr) -> AxVmResult<HostPhysAddr>,
) -> AxVmResult<Vec<HpaSegment>> {
    if size == 0 {
        return Err(ax_err_type!(
            InvalidData,
            "Cannot resolve an empty Guest DTB HPA range"
        ));
    }
    let mut cursor = gpa.as_usize();
    let end = cursor
        .checked_add(size)
        .ok_or_else(|| ax_err_type!(InvalidData, "Guest DTB GPA range overflows"))?;
    let mut segments: Vec<HpaSegment> = Vec::new();

    while cursor < end {
        let hpa = translate(GuestPhysAddr::from(cursor))?;
        let bytes_to_page_boundary = PAGE_SIZE_4K - cursor % PAGE_SIZE_4K;
        let length = (end - cursor).min(bytes_to_page_boundary);
        hpa.as_usize()
            .checked_add(length)
            .ok_or_else(|| ax_err_type!(InvalidData, "Guest DTB HPA translation overflows"))?;

        let contiguous = segments.last().is_some_and(|segment| {
            segment.start.as_usize().checked_add(segment.length) == Some(hpa.as_usize())
        });
        if contiguous {
            let segment = segments.last_mut().unwrap();
            segment.length = segment.length.checked_add(length).ok_or_else(|| {
                ax_err_type!(InvalidData, "Guest DTB HPA segment length overflows")
            })?;
        } else {
            segments.push(HpaSegment::new(hpa, length));
        }
        cursor = cursor
            .checked_add(length)
            .ok_or_else(|| ax_err_type!(InvalidData, "Guest DTB GPA cursor overflows"))?;
    }

    Ok(segments)
}

pub(crate) fn emit_ready_marker(vm: &AxVMRef, gpa: GuestPhysAddr, size: usize) -> AxVmResult {
    let hpa_segments = vm.guest_dtb_hpa_segments(gpa, size)?;
    let descriptor = GuestDtbEvidenceDescriptor::new(vm.id(), gpa, size, hpa_segments)?;
    // Colored host logs put their ANSI reset after their newline.  Establish a
    // fresh line under the shared console lock so the machine-readable marker
    // always starts at column zero and the strict capture planner need not
    // accept or strip terminal control bytes.
    ax_std::println!("\n{}", descriptor.marker());
    Ok(())
}

#[cfg(test)]
mod tests {
    use alloc::vec;

    use super::*;

    #[test]
    fn hpa_resolver_coalesces_only_contiguous_pages() {
        let segments = resolve_hpa_segments(GuestPhysAddr::from(0x1003), 0x2005, |gpa| {
            let hpa = match gpa.as_usize() {
                0x1003 => 0x8003,
                0x2000 => 0x9000,
                0x3000 => 0xb000,
                other => panic!("unexpected query GPA {other:#x}"),
            };
            Ok(HostPhysAddr::from(hpa))
        })
        .unwrap();

        assert_eq!(
            segments,
            vec![
                HpaSegment::new(HostPhysAddr::from(0x8003), 0x1ffd),
                HpaSegment::new(HostPhysAddr::from(0xb000), 8),
            ]
        );
    }

    #[test]
    fn descriptor_formats_strict_ready_marker() {
        let descriptor = GuestDtbEvidenceDescriptor::new(
            2,
            GuestPhysAddr::from(0x47e0_0000),
            8192,
            vec![
                HpaSegment::new(HostPhysAddr::from(0x87e0_0000), 4096),
                HpaSegment::new(HostPhysAddr::from(0x97e0_0000), 4096),
            ],
        )
        .unwrap();

        assert_eq!(
            descriptor.marker(),
            "AXVISOR_GUEST_DTB_READY vm=2 gpa=0x47e00000 size=8192 \
             hpa_segments=0x87e00000:4096,0x97e00000:4096"
        );
    }

    #[test]
    fn descriptor_rejects_incomplete_hpa_coverage() {
        assert!(
            GuestDtbEvidenceDescriptor::new(
                1,
                GuestPhysAddr::from(0x4000_0000),
                4096,
                vec![HpaSegment::new(HostPhysAddr::from(0x8000_0000), 2048)],
            )
            .is_err()
        );
    }

    #[test]
    fn hpa_resolver_rejects_unmapped_page() {
        assert!(
            resolve_hpa_segments(GuestPhysAddr::from(0x1000), 0x1001, |gpa| {
                if gpa.as_usize() == 0x1000 {
                    Ok(HostPhysAddr::from(0x8000))
                } else {
                    Err(ax_err_type!(InvalidData, "unmapped evidence page"))
                }
            })
            .is_err()
        );
    }
}
