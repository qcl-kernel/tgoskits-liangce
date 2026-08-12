#!/usr/bin/env python3

"""Keep the contest developer guide and the project handoff documents aligned."""

import hashlib
import re
import subprocess
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = WORKSPACE_ROOT.parent
GUIDE_RELATIVE_PATH = "docs/docs/development/contest-developer-guide.md"
GUIDE = WORKSPACE_ROOT / GUIDE_RELATIVE_PATH
SUBMISSION_GUIDE = (
    WORKSPACE_ROOT
    / "docs/docs/development/contest-repository-submission.md"
)
SPEC_RELATIVE_DIR = "docs/docs/development/contest-spec"
SPEC_DOCUMENTS = {
    "spec index": "README.md",
    "sources and decisions": "sources-and-decisions.md",
    "requirements": "requirements.md",
    "architecture": "architecture.md",
    "contracts": "contracts.md",
    "work packages": "work-packages.md",
    "test matrix": "test-matrix.md",
    "deliverables": "deliverables.md",
    "traceability": "traceability.md",
}
SOURCE_DOCUMENTS = {
    "陈天昊-良策-技术方案提纲.docx": (
        "bc13f31afe1ad0b1e20771379da4f666e77d97ec4d67700dfd8f6952d057029b"
    ),
    "陈天昊-良策-揭榜申请书.docx": (
        "4ea47d1b9c5fa8051302a8518a8cd48f2118886c28eeb0273df43922b4e99c33"
    ),
}
ROOT_DOCUMENT_NAMES = {
    "handoff": "开发交接.md",
    "plan": "计划.md",
    "status": "现状.md",
    "blockers": "阻塞.md",
}
README_DOCUMENTS = {
    "README.md": WORKSPACE_ROOT / "README.md",
    "README_CN.md": WORKSPACE_ROOT / "README_CN.md",
}
RUNBOOK = WORKSPACE_ROOT / "scripts" / "contest" / "README.md"
PROTOCOL = WORKSPACE_ROOT / "docs" / "docs" / "development" / "contest-icpc-protocol.md"
REQUIRED_WORK_PACKAGES = (
    "P2-DMA-01",
    "P2-DUAL-01",
    "P2-SOAK-01",
    "P3-RT-01",
    "P4-NET-01",
    "P5-AI-01",
    "P6-QUAL-01",
    "P7-DELIVER-01",
)
REQUIRED_REQUIREMENTS = (
    "REQ-PLAT-001",
    "REQ-PLAT-002",
    "REQ-PLAT-003",
    "REQ-RT-001",
    "REQ-RT-002",
    "REQ-RT-003",
    "REQ-NET-001",
    "REQ-NET-002",
    "REQ-NET-003",
    "REQ-NET-004",
    "REQ-AI-001",
    "REQ-AI-002",
    "REQ-AI-003",
    "REQ-QUAL-001",
    "REQ-QUAL-002",
    "REQ-DEL-001",
    "REQ-DEL-002",
    "REQ-DEL-003",
)
REQUIRED_DECISIONS = tuple(f"DEC-{index:03d}" for index in range(1, 10))
REQUIRED_ARCHITECTURE = tuple(f"ARC-{index:03d}" for index in range(1, 10))
REQUIRED_INTERFACES = tuple(f"IF-{index:03d}" for index in range(1, 12))
REQUIRED_TESTS = tuple(f"TEST-{index:03d}" for index in range(1, 23))
REQUIRED_DELIVERABLES = tuple(f"DEL-{index:03d}" for index in range(1, 13))
REQUIRED_KEY_PATHS = (
    "configs/contest/qemu-aarch64-baseline.env",
    "configs/contest/qemu-aarch64-linux-baseline.toml",
    "results/baseline/index.csv",
    "scripts/contest/capture_host_environment.ps1",
    "scripts/contest/icpc/icpc.c",
    "scripts/contest/icpc/test_icpc.c",
    "scripts/contest/run_axvisor_baseline.sh",
    "scripts/test/check_axvisor_dual_guest_soak_session.py",
    "scripts/test/check_axvisor_run_virtio_dma_effect_probe.py",
    "scripts/test/check_contest_baseline.py",
    "scripts/test/check_icpc_protocol.py",
)
STALE_PATTERNS = (
    r"先(?:跑|运行|执行)\s*r23",
    r"r23\s+remains\s+pending",
    r"r23\s*(?:仍|尚)(?:待|未)(?:运行|完成|验证)?",
)
BOUNDARY_PATTERNS = {
    "r23 does not prove DMA isolation": r"r23\s*(?:!=|≠|不等于|does not prove)\s*DMA\s+isolation",
    "r24d/r24e failed": r"r24d\s*/\s*r24e.*(?:failed|失败)",
    "static/single Guest does not prove dual/IP": (
        r"static\s*/\s*single\s+guest\s*(?:!=|≠|不等于|does not prove)\s*dual\s*/\s*IP"
    ),
}


def main() -> int:
    errors: list[str] = []
    check_guide_is_not_ignored(errors)
    guide = read_required_document(GUIDE, "contest developer guide", errors)
    specifications = read_specification_documents(errors)
    root_documents = read_optional_project_documents(errors)
    readmes = {
        label: read_required_document(path, label, errors)
        for label, path in README_DOCUMENTS.items()
    }
    runbook = read_required_document(RUNBOOK, "contest execution runbook", errors)
    protocol = read_required_document(PROTOCOL, "ICCP/ICPC protocol", errors)
    submission_guide = read_required_document(
        SUBMISSION_GUIDE, "contest repository submission guide", errors
    )

    check_root_documents(root_documents, errors)
    check_guide(guide, errors)
    check_specifications(specifications, errors)
    check_frozen_v1_design(specifications, guide, root_documents, errors)
    check_specification_links(specifications, errors)
    check_source_documents(specifications.get("sources and decisions", ""), errors)
    check_readmes(readmes, errors)
    check_runbook(runbook, errors)
    for fragment in (
        "qcl-kernel/tgoskits-liangce",
        "git push --mirror",
        "contest/axvisor-ai-control",
        "不创建 PR",
        "未执行",
    ):
        if fragment not in submission_guide:
            errors.append(
                "contest repository submission guide is missing: "
                f"{fragment}"
            )
    check_protocol_traceability(protocol, errors)
    check_key_paths(errors)
    check_stale_phrases(
        {"guide": guide, "runbook": runbook, **root_documents, **readmes}, errors
    )

    if not errors:
        return 0

    print("Contest developer documentation contract failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


def check_guide_is_not_ignored(errors: list[str]) -> None:
    if not GUIDE.is_file():
        return
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", GUIDE_RELATIVE_PATH],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        errors.append(f"contest developer guide is ignored: {GUIDE_RELATIVE_PATH}")
    elif result.returncode != 1:
        errors.append(
            "cannot determine whether the contest developer guide is ignored: "
            f"{result.stderr.strip()}"
        )

    for relative_name in SPEC_DOCUMENTS.values():
        relative_path = f"{SPEC_RELATIVE_DIR}/{relative_name}"
        path = WORKSPACE_ROOT / relative_path
        if not path.is_file():
            continue
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative_path],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            errors.append(f"contest specification document is ignored: {relative_path}")
        elif result.returncode != 1:
            errors.append(
                "cannot determine whether a contest specification document is ignored: "
                f"{relative_path}: {result.stderr.strip()}"
            )


def read_specification_documents(errors: list[str]) -> dict[str, str]:
    return {
        label: read_required_document(
            WORKSPACE_ROOT / SPEC_RELATIVE_DIR / relative_name, label, errors
        )
        for label, relative_name in SPEC_DOCUMENTS.items()
    }


def read_required_document(path: Path, label: str, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"{label} document is missing: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def read_optional_project_documents(errors: list[str]) -> dict[str, str]:
    """Check the local parent-directory handoff set when that workspace exists.

    The four process documents intentionally live outside the Git repository.
    A normal CI checkout therefore has none of them, while this contest
    workspace must have the complete set.  A partial set is always an error.
    """

    paths = {
        label: PROJECT_ROOT / name for label, name in ROOT_DOCUMENT_NAMES.items()
    }
    present = {label: path.is_file() for label, path in paths.items()}
    if not any(present.values()):
        return {}
    missing = [label for label, exists in present.items() if not exists]
    if missing:
        errors.append(
            "local project document set is incomplete: " + ", ".join(sorted(missing))
        )
    return {
        label: read_required_document(path, label, errors)
        for label, path in paths.items()
        if path.is_file()
    }


def check_root_documents(documents: dict[str, str], errors: list[str]) -> None:
    for label, content in documents.items():
        if "2026-08-12" not in content:
            errors.append(f"{label} document does not record the 2026-08-12 update")
        if GUIDE_RELATIVE_PATH not in content:
            errors.append(f"{label} document does not link to or mention the developer guide")
        if f"{SPEC_RELATIVE_DIR}/README.md" not in content:
            errors.append(f"{label} document does not link to the original-task specification")
        if "P2-DMA-01" not in content:
            errors.append(f"{label} document does not include work package P2-DMA-01")


def check_guide(guide: str, errors: list[str]) -> None:
    if "contest-spec/README.md" not in guide:
        errors.append("contest developer guide does not link to the original-task specification")
    for work_package in REQUIRED_WORK_PACKAGES:
        if work_package not in guide:
            errors.append(f"contest developer guide is missing work package {work_package}")
    for boundary, pattern in BOUNDARY_PATTERNS.items():
        if not re.search(pattern, guide, flags=re.IGNORECASE | re.DOTALL):
            errors.append(f"contest developer guide does not state that {boundary}")


def check_specifications(specifications: dict[str, str], errors: list[str]) -> None:
    index = specifications.get("spec index", "")
    for relative_name in SPEC_DOCUMENTS.values():
        if relative_name == "README.md":
            continue
        if relative_name not in index:
            errors.append(f"contest specification index does not link to {relative_name}")

    decisions = specifications.get("sources and decisions", "")
    requirements = specifications.get("requirements", "")
    architecture = specifications.get("architecture", "")
    contracts = specifications.get("contracts", "")
    work_packages = specifications.get("work packages", "")
    test_matrix = specifications.get("test matrix", "")
    deliverables = specifications.get("deliverables", "")
    traceability = specifications.get("traceability", "")
    for label, content in specifications.items():
        if "2026-08-12" not in content:
            errors.append(f"{label} document does not record the 2026-08-12 update")
    for decision in REQUIRED_DECISIONS:
        if decision not in decisions:
            errors.append(f"sources and decisions document is missing {decision}")
    for requirement in REQUIRED_REQUIREMENTS:
        if requirement not in requirements:
            errors.append(f"requirements document is missing {requirement}")
        if requirement not in traceability:
            errors.append(f"traceability document is missing {requirement}")
    for work_package in REQUIRED_WORK_PACKAGES:
        if work_package not in work_packages:
            errors.append(f"work-package specification is missing {work_package}")
        if work_package not in traceability:
            errors.append(f"traceability document is missing {work_package}")
    for component in REQUIRED_ARCHITECTURE:
        if component not in architecture:
            errors.append(f"architecture document is missing {component}")
        if component not in traceability:
            errors.append(f"traceability document is missing {component}")
    for interface in REQUIRED_INTERFACES:
        if interface not in contracts:
            errors.append(f"contracts document is missing {interface}")
        if interface not in traceability:
            errors.append(f"traceability document is missing {interface}")
    for test in REQUIRED_TESTS:
        if test not in test_matrix:
            errors.append(f"test matrix is missing {test}")
        if test not in traceability:
            errors.append(f"traceability document is missing {test}")
    for deliverable in REQUIRED_DELIVERABLES:
        if deliverable not in deliverables:
            errors.append(f"deliverables document is missing {deliverable}")
        if deliverable not in traceability:
            errors.append(f"traceability document is missing {deliverable}")

    required_boundaries = (
        "r23",
        "r24d",
        "r24e",
        "host ICPC",
        "single-Guest",
        "dual-Guest",
        "DMA isolation",
    )
    for boundary in required_boundaries:
        if boundary.lower() not in traceability.lower():
            errors.append(f"traceability document is missing evidence boundary: {boundary}")


def check_frozen_v1_design(
    specifications: dict[str, str],
    guide: str,
    root_documents: dict[str, str],
    errors: list[str],
) -> None:
    """Prevent the decided v1 branches from drifting back into open choices."""

    decisions = specifications.get("sources and decisions", "")
    architecture = specifications.get("architecture", "")
    contracts = specifications.get("contracts", "")
    work_packages = specifications.get("work packages", "")
    test_matrix = specifications.get("test matrix", "")
    traceability = specifications.get("traceability", "")
    requirements = specifications.get("requirements", "")
    deliverables = specifications.get("deliverables", "")

    decision_fragments = (
        "Linux-only v1",
        "Zephyr-only v1",
        "QEMU AArch64-only v1",
        "ICCP 公共名称与 ICPC v1 wire",
        "UDP:46000 主传输，TCP:46001",
        "软件拷贝 mediated VirtIO-net",
        "3→8→1",
        "2026-08-20 18:00 Asia/Shanghai",
        "禁止创建或提交 PR",
    )
    for fragment in decision_fragments:
        if fragment not in decisions:
            errors.append(f"frozen v1 decision is missing: {fragment}")
    if decisions.count("adopted-for-v1-design") < len(REQUIRED_DECISIONS):
        errors.append("not every DEC-001..009 is adopted as a v1 design input")

    cross_document_fragments = {
        "architecture": (
            "vnet0",
            "0x0a000200",
            "0x0a000400",
            "1514",
            "任何 IPv6",
            "3→8→1",
            "100 ms",
        ),
        "contracts": (
            "vnet0",
            "10.77.0.1/24",
            "10.77.0.2/24",
            "46000",
            "46001",
            "任何 IPv6",
            "3→8→1",
            "500 ms",
        ),
        "work packages": (
            "P4-NET-A",
            "P4-NET-F",
            "slot2 passthrough",
            "P2 专属 request/session/result schema",
            "VIRTIO_F_VERSION_1",
            "VIRTIO_NET_F_STATUS",
            "VIRTIO_NET_F_MTU",
            "drop-newest",
        ),
        "requirements": (
            "REQ-DEL-002",
            "qcl-kernel/tgoskits-liangce",
            "git format-patch",
            "禁止创建或提交 PR",
        ),
        "test matrix": ("10,000", "3→8→1", "-150 mC/tick", "not-created-by-policy"),
        "traceability": ("mediated VirtIO-net", "设计冻结", "禁止创建或提交 PR"),
        "deliverables": (
            "DEL-010",
            "qcl-kernel/tgoskits-liangce",
            "prPolicy=forbidden",
            "git am",
        ),
        "guide": (
            "mediated VirtIO-net",
            "3→8→1",
            "P4-NET-01 -> P5-AI-01",
            "P3-RT-01 production A/B",
            "2026-08-20 18:00",
            "qcl-kernel/tgoskits-liangce",
            "禁止创建或提交 PR",
        ),
    }
    contents = {
        "architecture": architecture,
        "contracts": contracts,
        "work packages": work_packages,
        "requirements": requirements,
        "test matrix": test_matrix,
        "traceability": traceability,
        "deliverables": deliverables,
        "guide": guide,
    }
    for label, fragments in cross_document_fragments.items():
        for fragment in fragments:
            if fragment not in contents[label]:
                errors.append(f"{label} does not carry frozen v1 value: {fragment}")

    stale_choice_patterns = (
        r"温度(?:一阶对象)?\s*或\s*电机",
        r"温度或转速",
        r"若工期(?:紧张|不足).*改用\s*TCP",
        r"5→16→8→1",
        r"控制周期(?:固定为|为|=)?\s*10\s*ms",
        r"RISC-V\s*是否移出最终基础交付仍待",
        r"实现并批准\s*`?IF-003/004`?",
        r"通过\s*`?IF-006/DEC-004`?\s*冻结",
        r"\|\s*`P2-DMA-01`[^\n]*`IF-002`",
        r"IPv4/IPv6\s+multicast",
        r"之后并行推进\s*P3-RT-01\s*与\s*P4-NET-01",
        r"\|->\s*P3-RT-01",
        r"`VERSION_1`、`MAC`\s*和已经实现/验证的最小\s*feature",
    )
    authoritative = {
        "guide": guide,
        "architecture": architecture,
        "contracts": contracts,
        "work packages": work_packages,
        **root_documents,
    }
    for label, content in authoritative.items():
        for pattern in stale_choice_patterns:
            if re.search(pattern, content, flags=re.IGNORECASE):
                errors.append(
                    f"{label} retains an open choice superseded by DEC-001..009: {pattern}"
                )

    for label, content in root_documents.items():
        if "禁止创建或提交 PR" not in content:
            errors.append(f"{label} does not carry the no-PR delivery policy")


def check_specification_links(
    specifications: dict[str, str], errors: list[str]
) -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for label, content in specifications.items():
        source_path = WORKSPACE_ROOT / SPEC_RELATIVE_DIR / SPEC_DOCUMENTS[label]
        for target in link_pattern.findall(content):
            if target.startswith(("http://", "https://", "#")):
                continue
            relative_target = target.split("#", maxsplit=1)[0]
            if not relative_target:
                continue
            resolved_target = (source_path.parent / relative_target).resolve()
            if not resolved_target.exists():
                errors.append(
                    f"{label} document has a broken local link: {target}"
                )


def check_source_documents(sources: str, errors: list[str]) -> None:
    for name, expected_hash in SOURCE_DOCUMENTS.items():
        if name not in sources or expected_hash not in sources:
            errors.append(f"sources and decisions document does not bind {name}")

    present = {
        name: PROJECT_ROOT / name
        for name in SOURCE_DOCUMENTS
        if (PROJECT_ROOT / name).is_file()
    }
    if present and len(present) != len(SOURCE_DOCUMENTS):
        errors.append("local original application document set is incomplete")
    for name, path in present.items():
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        expected_hash = SOURCE_DOCUMENTS[name]
        if actual_hash != expected_hash:
            errors.append(
                f"original application document hash changed for {name}: "
                f"expected {expected_hash}, got {actual_hash}"
            )


def check_readmes(readmes: dict[str, str], errors: list[str]) -> None:
    for label, content in readmes.items():
        if GUIDE_RELATIVE_PATH not in content:
            errors.append(f"{label} does not provide an entry to the contest developer guide")
        if f"{SPEC_RELATIVE_DIR}/README.md" not in content:
            errors.append(f"{label} does not provide an entry to the contest specification")
        if "├── platforms/" not in content or "├── platform/" in content:
            errors.append(f"{label} does not name the actual platforms/ directory")
        no_pr_fragment = (
            "creating or submitting a PR is forbidden"
            if label == "README.md"
            else "禁止创建或提交 PR"
        )
        if no_pr_fragment not in content:
            errors.append(f"{label} does not state the contest no-PR policy")


def check_runbook(runbook: str, errors: list[str]) -> None:
    required_fragments = (
        "--device-id dma-probe --bus virtio-mmio-bus.1",
        "--guest-device /dev/vdb --drive-id dma-probe-disk",
        "--control-socket /tmp/axdma-<nonce>/control.sock",
        "--raw-log <new-run>/live/observed-log-prefix.bin",
        "Linux `[0,1]`, Zephyr `[2]`",
        "not a QEMU launcher or",
    )
    for fragment in required_fragments:
        if fragment not in runbook:
            errors.append(f"contest execution runbook is missing: {fragment}")
    if "fixed mutually exclusive CPU sets (Linux `[0]`, Zephyr `[1]`)" in runbook:
        errors.append("contest execution runbook retains the obsolete dual-Guest CPU map")


def check_protocol_traceability(protocol: str, errors: list[str]) -> None:
    required_fragments = (
        "申报材料把应用协议称为 `ICCP`",
        "仓库当前源码、线格式 magic、测试和本文使用 `ICPC v1`",
        "`DEC-004`",
        "`flags` | `u16` | `flags: u8`",
        "`timestamp_ns` | `u64` | `timestamp_ms: u64`",
        "`payload_len` | `u32` | `payload_length: u16`",
        "`error_code` | `i32` | `error_code: u16`",
    )
    for fragment in required_fragments:
        if fragment not in protocol:
            errors.append(f"ICCP/ICPC protocol traceability is missing: {fragment}")


def check_key_paths(errors: list[str]) -> None:
    for relative_path in REQUIRED_KEY_PATHS:
        if not (WORKSPACE_ROOT / relative_path).is_file():
            errors.append(f"contest developer guide key path is missing: {relative_path}")


def check_stale_phrases(documents: dict[str, str], errors: list[str]) -> None:
    for label, content in documents.items():
        for pattern in STALE_PATTERNS:
            if re.search(pattern, content, flags=re.IGNORECASE):
                errors.append(f"{label} document retains stale r23 next-step wording: {pattern}")


if __name__ == "__main__":
    sys.exit(main())
