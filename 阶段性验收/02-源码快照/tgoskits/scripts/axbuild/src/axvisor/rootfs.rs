//! Axvisor-specific rootfs resolution and preparation helpers.
//!
//! Main responsibilities:
//! - Resolve which rootfs image Axvisor should use for a QEMU run
//! - Distinguish between explicit, managed, and VM-config-derived rootfs paths
//! - Prepare managed rootfs images before launch when Axvisor relies on them
//! - Patch QEMU configs with the selected rootfs using Axvisor-specific rules

use std::{
    fs,
    path::{Path, PathBuf},
};

use anyhow::{anyhow, bail};
use ostool::{build::config::Cargo, run::qemu::QemuConfig};
use serde::Deserialize;

use super::{Axvisor, build};
use crate::{context::ResolvedAxvisorRequest, rootfs};

#[derive(Deserialize)]
struct VmRootfsProbe {
    kernel: Option<VmKernelRootfsProbe>,
}

#[derive(Deserialize)]
struct VmKernelRootfsProbe {
    kernel_path: Option<String>,
}

pub(super) async fn qemu(axvisor: &mut Axvisor, args: super::ArgsQemu) -> anyhow::Result<()> {
    let request = axvisor.prepare_request(
        (&args.build).into(),
        args.qemu_config,
        None,
        crate::context::SnapshotPersistence::Store,
    )?;
    axvisor.app.set_debug_mode(request.debug)?;
    let explicit_rootfs = args
        .rootfs
        .map(|rootfs| {
            crate::image::storage::resolve_explicit_rootfs(
                axvisor.app.workspace_root(),
                &request.arch,
                rootfs,
            )
        })
        .transpose()?;
    ensure_qemu_rootfs_ready(
        &request,
        axvisor.app.workspace_root(),
        explicit_rootfs.as_deref(),
    )
    .await?;
    let mut cargo = build::load_cargo_config(&request)?;
    let mut qemu =
        load_patched_qemu_config(axvisor, &request, &cargo, explicit_rootfs.as_deref()).await?;
    patch_live_capture_identity_args(
        &mut qemu,
        args.qmp_socket.as_deref(),
        args.qemu_pidfile.as_deref(),
        args.qemu_name.as_deref(),
    )?;
    cargo.to_bin = qemu_to_bin_requested(&qemu)?;
    axvisor
        .app
        .qemu(cargo, request.build_info_path, Some(qemu))
        .await
}

fn patch_live_capture_identity_args(
    qemu: &mut QemuConfig,
    qmp_socket: Option<&Path>,
    qemu_pidfile: Option<&Path>,
    qemu_name: Option<&str>,
) -> anyhow::Result<()> {
    let (Some(qmp_socket), Some(qemu_pidfile), Some(qemu_name)) =
        (qmp_socket, qemu_pidfile, qemu_name)
    else {
        if qmp_socket.is_some() || qemu_pidfile.is_some() || qemu_name.is_some() {
            bail!("--qmp-socket, --qemu-pidfile, and --qemu-name must be provided together");
        }
        return Ok(());
    };

    if !qmp_socket.is_absolute() || !qemu_pidfile.is_absolute() {
        bail!("live-capture QMP socket and pidfile paths must be absolute");
    }
    if qmp_socket == qemu_pidfile {
        bail!("live-capture QMP socket and pidfile paths must be different");
    }
    let qmp_socket = qmp_socket
        .to_str()
        .ok_or_else(|| anyhow!("live-capture QMP socket path is not UTF-8"))?;
    let qemu_pidfile = qemu_pidfile
        .to_str()
        .ok_or_else(|| anyhow!("live-capture QEMU pidfile path is not UTF-8"))?;
    if qmp_socket.as_bytes().len() > 100 {
        bail!("live-capture QMP socket path exceeds the bounded Unix-socket length");
    }
    for (label, value) in [("QMP socket", qmp_socket), ("QEMU pidfile", qemu_pidfile)] {
        if value.contains(',') || value.chars().any(char::is_control) {
            bail!("live-capture {label} path contains a forbidden delimiter");
        }
    }
    let nonce = ["axvisor-guest-dtb-", "axvisor-dma-effect-"]
        .into_iter()
        .find_map(|prefix| qemu_name.strip_prefix(prefix))
        .ok_or_else(|| anyhow!("identity-bound QEMU name has the wrong prefix"))?;
    if nonce.len() != 32
        || !nonce
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        bail!("identity-bound QEMU name must end in a 128-bit lowercase hexadecimal nonce");
    }

    for argument in &qemu.args {
        if argument
            .chars()
            .any(|character| matches!(character, '\0' | '\r' | '\n'))
        {
            bail!("raw QEMU argument contains a forbidden control character");
        }
        for token in argument.split_ascii_whitespace() {
            if ["-qmp", "-pidfile", "-name"]
                .iter()
                .any(|option| token == *option || token.starts_with(&format!("{option}=")))
            {
                bail!("raw QEMU args conflict with the identity-bound live-capture option {token}");
            }
        }
    }

    qemu.args.extend([
        "-qmp".to_string(),
        format!("unix:{qmp_socket},server=on,wait=off"),
        "-pidfile".to_string(),
        qemu_pidfile.to_string(),
        "-name".to_string(),
        qemu_name.to_string(),
    ]);
    Ok(())
}

fn qemu_to_bin_requested(qemu: &QemuConfig) -> anyhow::Result<bool> {
    if qemu.uefi && !qemu.to_bin {
        bail!(
            "QEMU config enables UEFI but does not request `to_bin = true`; set `to_bin = true` \
             explicitly"
        );
    }
    Ok(qemu.to_bin)
}

pub(super) async fn load_patched_qemu_config(
    axvisor: &mut Axvisor,
    request: &ResolvedAxvisorRequest,
    cargo: &Cargo,
    explicit_rootfs: Option<&Path>,
) -> anyhow::Result<QemuConfig> {
    let config_path = request.qemu_config.clone().unwrap_or_else(|| {
        super::default_qemu_config_template_path(&request.axvisor_dir, &request.arch)
    });
    let mut qemu = axvisor
        .app
        .read_qemu_config_from_path_for_cargo(cargo, &config_path)
        .await?;
    patch_qemu_rootfs(
        &mut qemu,
        request,
        axvisor.app.workspace_root(),
        explicit_rootfs,
    )?;
    Ok(qemu)
}

/// Ensures the managed rootfs required by an Axvisor QEMU run is available.
pub(crate) async fn ensure_qemu_rootfs_ready(
    request: &ResolvedAxvisorRequest,
    workspace_root: &Path,
    explicit_rootfs: Option<&Path>,
) -> anyhow::Result<()> {
    let rootfs_path = managed_rootfs_path(request, workspace_root, explicit_rootfs)?;
    crate::image::storage::ensure_optional_managed_rootfs(
        workspace_root,
        &request.arch,
        rootfs_path.as_deref(),
    )
    .await
}

/// Patches a QEMU config with the rootfs selected for an Axvisor request.
pub(crate) fn patch_qemu_rootfs(
    config: &mut QemuConfig,
    request: &ResolvedAxvisorRequest,
    workspace_root: &Path,
    explicit_rootfs: Option<&Path>,
) -> anyhow::Result<()> {
    let rootfs_path = qemu_rootfs_path(request, workspace_root, explicit_rootfs)?;
    patch_qemu_rootfs_path(config, &rootfs_path);
    Ok(())
}

/// Resolves the rootfs path selected for an Axvisor QEMU request.
pub(crate) fn qemu_rootfs_path(
    request: &ResolvedAxvisorRequest,
    workspace_root: &Path,
    explicit_rootfs: Option<&Path>,
) -> anyhow::Result<PathBuf> {
    if let Some(explicit) = explicit_rootfs {
        return Ok(explicit.to_path_buf());
    }

    infer_rootfs_path(&request.vmconfigs)?
        .map(Ok)
        .unwrap_or_else(|| {
            crate::image::storage::default_rootfs_path(workspace_root, &request.arch)
        })
}

/// Patches a QEMU config with a concrete Axvisor rootfs path.
pub(crate) fn patch_qemu_rootfs_path(config: &mut QemuConfig, rootfs_path: &Path) {
    rootfs::qemu::patch_rootfs(
        config,
        rootfs_path,
        rootfs::qemu::RootfsPatchMode::ReplaceDriveOnly,
    );
}

/// Returns the managed rootfs path Axvisor should prepare, if any.
pub(crate) fn managed_rootfs_path(
    request: &ResolvedAxvisorRequest,
    workspace_root: &Path,
    explicit_rootfs: Option<&Path>,
) -> anyhow::Result<Option<PathBuf>> {
    if let Some(explicit_rootfs) = explicit_rootfs {
        return crate::image::storage::resolve_managed_rootfs_path(workspace_root, explicit_rootfs);
    }

    if infer_rootfs_path(&request.vmconfigs)?.is_none() {
        return Ok(Some(crate::image::storage::default_rootfs_path(
            workspace_root,
            &request.arch,
        )?));
    }

    Ok(None)
}

/// Infers a rootfs image path from VM config files by looking next to the
/// configured guest kernel image.
pub(crate) fn infer_rootfs_path(vmconfigs: &[PathBuf]) -> anyhow::Result<Option<PathBuf>> {
    for vmconfig in vmconfigs {
        let content = fs::read_to_string(vmconfig)
            .map_err(|e| anyhow!("failed to read vm config {}: {e}", vmconfig.display()))?;
        let probe: VmRootfsProbe = toml::from_str(&content)
            .map_err(|e| anyhow!("failed to parse vm config {}: {e}", vmconfig.display()))?;
        let Some(kernel_path) = probe.kernel.and_then(|kernel| kernel.kernel_path) else {
            continue;
        };
        let rootfs_path = Path::new(&kernel_path)
            .parent()
            .map(|dir| dir.join("rootfs.img"));
        if let Some(rootfs_path) = rootfs_path
            && rootfs_path.exists()
        {
            return Ok(Some(rootfs_path));
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use tempfile::tempdir;

    use super::*;

    fn managed_rootfs_path_for_test(root: &Path, image_name: &str) -> PathBuf {
        root.join(".tgos-images").join(image_name).join(image_name)
    }

    fn write_test_image_config(root: &Path) {
        let config = crate::image::config::ImageConfig {
            local_storage: root.join(".tgos-images"),
            registry: crate::image::config::DEFAULT_REGISTRY_URL.to_string(),
            auto_sync: true,
            auto_sync_threshold: 60,
        };
        crate::image::config::ImageConfig::write_config(root, &config).unwrap();
    }

    fn request(root: &Path, vmconfigs: Vec<PathBuf>) -> ResolvedAxvisorRequest {
        ResolvedAxvisorRequest {
            package: crate::axvisor::build::AXVISOR_PACKAGE.to_string(),
            axvisor_dir: root.join("os/axvisor"),
            arch: "aarch64".to_string(),
            target: "aarch64-unknown-none-softfloat".to_string(),
            smp: None,
            debug: false,
            build_info_path: root.join(".build.toml"),
            qemu_config: None,
            uboot_config: None,
            vmconfigs,
        }
    }

    #[test]
    fn infer_rootfs_path_uses_vmconfig_kernel_sibling() {
        let root = tempdir().unwrap();
        let image_dir = root.path().join("image");
        fs::create_dir_all(&image_dir).unwrap();
        fs::write(image_dir.join("rootfs.img"), b"rootfs").unwrap();
        let vmconfig = root.path().join("vm.toml");
        fs::write(
            &vmconfig,
            format!(
                r#"
[kernel]
kernel_path = "{}"
"#,
                image_dir.join("qemu-aarch64").display()
            ),
        )
        .unwrap();

        assert_eq!(
            infer_rootfs_path(&[vmconfig]).unwrap(),
            Some(image_dir.join("rootfs.img"))
        );
    }

    #[test]
    fn infer_rootfs_path_skips_vmconfig_without_kernel_path() {
        let root = tempdir().unwrap();
        let vmconfig = root.path().join("vm.toml");
        fs::write(
            &vmconfig,
            r#"
[kernel]
cmdline = "console=ttyS0"
"#,
        )
        .unwrap();

        assert_eq!(infer_rootfs_path(&[vmconfig]).unwrap(), None);
    }

    #[test]
    fn infer_rootfs_path_skips_nonexistent_kernel_sibling_rootfs() {
        let root = tempdir().unwrap();
        let image_dir = root.path().join("image");
        fs::create_dir_all(&image_dir).unwrap();
        let vmconfig = root.path().join("vm.toml");
        fs::write(
            &vmconfig,
            format!(
                r#"
[kernel]
kernel_path = "{}"
"#,
                image_dir.join("qemu-aarch64").display()
            ),
        )
        .unwrap();

        assert_eq!(infer_rootfs_path(&[vmconfig]).unwrap(), None);
    }

    #[test]
    fn patch_qemu_rootfs_overrides_rootfs_when_vmconfig_provides_one() {
        let root = tempdir().unwrap();
        let image_dir = root.path().join("image");
        fs::create_dir_all(&image_dir).unwrap();
        let rootfs_path = image_dir.join("rootfs.img");
        fs::write(&rootfs_path, b"rootfs").unwrap();
        let vmconfig = root.path().join("vm.toml");
        fs::write(
            &vmconfig,
            format!(
                r#"
[kernel]
kernel_path = "{}"
"#,
                image_dir.join("qemu-aarch64").display()
            ),
        )
        .unwrap();

        let mut qemu = QemuConfig {
            args: vec!["id=disk0,if=none,format=raw,file=/old/tmp/rootfs.img".to_string()],
            ..Default::default()
        };
        patch_qemu_rootfs(
            &mut qemu,
            &request(root.path(), vec![vmconfig]),
            root.path(),
            None,
        )
        .unwrap();

        assert_eq!(
            qemu.args,
            vec![format!(
                "id=disk0,if=none,format=raw,file={}",
                rootfs_path.display()
            )]
        );
    }

    #[test]
    fn patch_qemu_rootfs_preserves_dma_probe_and_control_topology() {
        let root = tempdir().unwrap();
        let explicit_rootfs = root.path().join("explicit-rootfs.ext4");
        let probe_drive =
            "id=dma-probe-disk,if=none,format=raw,readonly=on,file=/run/dma-probe.raw";
        let probe_device =
            "virtio-blk-device,id=dma-probe,drive=dma-probe-disk,bus=virtio-mmio-bus.1";
        let control_chardev =
            "socket,id=dma-go-chardev,path=/tmp/axdma-test/control.sock,server=on,wait=off";
        let control_serial = "virtio-serial-device,id=dma-go-serial,bus=virtio-mmio-bus.2";
        let control_port = "virtconsole,id=dma-go-port,chardev=dma-go-chardev,name=dma-go";
        let mut qemu = QemuConfig {
            args: vec![
                "-drive".to_string(),
                "id=disk0,if=none,format=raw,file=/run/old-rootfs.ext4".to_string(),
                "-device".to_string(),
                "virtio-blk-device,id=linux-root,drive=disk0,bus=virtio-mmio-bus.0".to_string(),
                "-drive".to_string(),
                probe_drive.to_string(),
                "-device".to_string(),
                probe_device.to_string(),
                "-chardev".to_string(),
                control_chardev.to_string(),
                "-device".to_string(),
                control_serial.to_string(),
                "-device".to_string(),
                control_port.to_string(),
            ],
            ..Default::default()
        };

        patch_qemu_rootfs(
            &mut qemu,
            &request(root.path(), vec![]),
            root.path(),
            Some(&explicit_rootfs),
        )
        .unwrap();

        assert_eq!(
            qemu.args,
            vec![
                "-drive".to_string(),
                format!(
                    "id=disk0,if=none,format=raw,file={}",
                    explicit_rootfs.display()
                ),
                "-device".to_string(),
                "virtio-blk-device,id=linux-root,drive=disk0,bus=virtio-mmio-bus.0".to_string(),
                "-drive".to_string(),
                probe_drive.to_string(),
                "-device".to_string(),
                probe_device.to_string(),
                "-chardev".to_string(),
                control_chardev.to_string(),
                "-device".to_string(),
                control_serial.to_string(),
                "-device".to_string(),
                control_port.to_string(),
            ]
        );
    }

    #[test]
    fn patch_qemu_rootfs_uses_unified_rootfs_by_default() {
        let root = tempdir().unwrap();
        write_test_image_config(root.path());
        let rootfs = managed_rootfs_path_for_test(root.path(), "rootfs-aarch64-alpine.img");
        let mut qemu = QemuConfig {
            args: vec!["id=disk0,if=none,format=raw,file=/old/tmp/rootfs.img".to_string()],
            ..Default::default()
        };

        patch_qemu_rootfs(&mut qemu, &request(root.path(), vec![]), root.path(), None).unwrap();

        assert_eq!(
            qemu.args,
            vec![format!(
                "id=disk0,if=none,format=raw,file={}",
                rootfs.display()
            )]
        );
    }

    #[test]
    fn patch_qemu_rootfs_inserts_drive_arg_when_template_omits_it() {
        let root = tempdir().unwrap();
        write_test_image_config(root.path());
        let rootfs = managed_rootfs_path_for_test(root.path(), "rootfs-aarch64-alpine.img");
        let mut qemu = QemuConfig {
            args: vec![
                "-device".to_string(),
                "virtio-blk-device,drive=disk0".to_string(),
                "-append".to_string(),
                "root=/dev/vda rw init=/bin/sh".to_string(),
            ],
            ..Default::default()
        };

        patch_qemu_rootfs(&mut qemu, &request(root.path(), vec![]), root.path(), None).unwrap();

        assert_eq!(
            qemu.args,
            vec![
                "-device".to_string(),
                "virtio-blk-device,drive=disk0".to_string(),
                "-drive".to_string(),
                format!("id=disk0,if=none,format=raw,file={}", rootfs.display()),
                "-append".to_string(),
                "root=/dev/vda rw init=/bin/sh".to_string(),
            ]
        );
    }

    #[test]
    fn managed_rootfs_path_uses_default_unified_rootfs_when_vmconfig_has_no_rootfs() {
        let root = tempdir().unwrap();
        write_test_image_config(root.path());
        let vmconfig = root.path().join("vm.toml");
        fs::write(
            &vmconfig,
            r#"
[kernel]
kernel_path = "/tmp/qemu-aarch64"
"#,
        )
        .unwrap();

        assert_eq!(
            managed_rootfs_path(&request(root.path(), vec![vmconfig]), root.path(), None).unwrap(),
            Some(managed_rootfs_path_for_test(
                root.path(),
                "rootfs-aarch64-alpine.img"
            ))
        );
    }

    #[test]
    fn managed_rootfs_path_skips_when_vmconfig_provides_kernel_sibling_rootfs() {
        let root = tempdir().unwrap();
        let image_dir = root.path().join("image");
        fs::create_dir_all(&image_dir).unwrap();
        fs::write(image_dir.join("rootfs.img"), b"rootfs").unwrap();
        let vmconfig = root.path().join("vm.toml");
        fs::write(
            &vmconfig,
            format!(
                r#"
[kernel]
kernel_path = "{}"
"#,
                image_dir.join("qemu-aarch64").display()
            ),
        )
        .unwrap();

        assert_eq!(
            managed_rootfs_path(&request(root.path(), vec![vmconfig]), root.path(), None).unwrap(),
            None
        );
    }

    #[test]
    fn managed_rootfs_path_keeps_explicit_managed_rootfs() {
        let root = tempdir().unwrap();
        write_test_image_config(root.path());
        let explicit = managed_rootfs_path_for_test(root.path(), "rootfs-aarch64-debian.img");

        assert_eq!(
            managed_rootfs_path(
                &request(root.path(), vec![]),
                root.path(),
                Some(explicit.as_path())
            )
            .unwrap(),
            Some(explicit)
        );
    }

    #[test]
    fn qemu_uefi_without_to_bin_is_rejected() {
        let qemu = QemuConfig {
            uefi: true,
            to_bin: false,
            ..Default::default()
        };

        assert!(qemu_to_bin_requested(&qemu).is_err());
    }

    fn absolute_test_path(name: &str) -> PathBuf {
        if cfg!(windows) {
            PathBuf::from(format!(r"C:\axvisor-live-capture\{name}"))
        } else {
            PathBuf::from(format!("/tmp/axvisor-live-capture/{name}"))
        }
    }

    #[test]
    fn live_capture_identity_args_are_exact_and_ordered() {
        let socket = absolute_test_path("qmp.sock");
        let pidfile = absolute_test_path("qemu.pid");
        let name = "axvisor-guest-dtb-0123456789abcdef0123456789abcdef";
        let mut qemu = QemuConfig {
            args: vec!["-nographic".to_string()],
            ..Default::default()
        };

        patch_live_capture_identity_args(&mut qemu, Some(&socket), Some(&pidfile), Some(name))
            .unwrap();

        assert_eq!(
            &qemu.args[1..],
            &[
                "-qmp".to_string(),
                format!("unix:{},server=on,wait=off", socket.display()),
                "-pidfile".to_string(),
                pidfile.display().to_string(),
                "-name".to_string(),
                name.to_string(),
            ]
        );
    }

    #[test]
    fn dma_effect_identity_name_is_accepted_without_weakening_the_nonce() {
        let socket = absolute_test_path("dma-qmp.sock");
        let pidfile = absolute_test_path("dma-qemu.pid");
        let name = "axvisor-dma-effect-0123456789abcdef0123456789abcdef";
        let mut qemu = QemuConfig {
            args: vec!["-nographic".to_string()],
            ..Default::default()
        };

        patch_live_capture_identity_args(&mut qemu, Some(&socket), Some(&pidfile), Some(name))
            .unwrap();

        assert_eq!(qemu.args.last().map(String::as_str), Some(name));
        let weak = "axvisor-dma-effect-0123456789abcdef";
        assert!(
            patch_live_capture_identity_args(
                &mut QemuConfig::default(),
                Some(&socket),
                Some(&pidfile),
                Some(weak),
            )
            .is_err()
        );
    }

    #[test]
    fn live_capture_identity_args_reject_partial_and_raw_conflicts() {
        let socket = absolute_test_path("qmp.sock");
        let pidfile = absolute_test_path("qemu.pid");
        let name = "axvisor-guest-dtb-0123456789abcdef0123456789abcdef";
        let mut partial = QemuConfig::default();
        assert!(
            patch_live_capture_identity_args(&mut partial, Some(&socket), None, Some(name))
                .is_err()
        );

        for conflict in ["-qmp", "-pidfile=/tmp/other.pid", "-name old"] {
            let mut qemu = QemuConfig {
                args: vec![conflict.to_string()],
                ..Default::default()
            };
            assert!(
                patch_live_capture_identity_args(
                    &mut qemu,
                    Some(&socket),
                    Some(&pidfile),
                    Some(name),
                )
                .is_err(),
                "raw conflict {conflict} was accepted"
            );
        }
    }

    #[test]
    fn live_capture_identity_args_reject_weak_name_and_path_injection() {
        let socket = absolute_test_path("qmp,sock");
        let pidfile = absolute_test_path("qemu.pid");
        let mut qemu = QemuConfig::default();
        assert!(
            patch_live_capture_identity_args(
                &mut qemu,
                Some(&socket),
                Some(&pidfile),
                Some("axvisor-guest-dtb-not-a-nonce"),
            )
            .is_err()
        );
    }
}
