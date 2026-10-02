"""The run seccomp profile (hosted-sandbox-isolation spec §10.2).

A default-allow filter that denies the syscalls container escapes are built
from: tracing other processes, mounting, entering or creating namespaces,
kernel keyrings, BPF, userfaultfd, perf, kexec and module loading, plus the
large-attack-surface interfaces a build or test never needs (io_uring, the new
mount API, handle-based opens). bubblewrap applies it right before exec'ing
the run's first process — after its own namespace setup — so denying
`mount`/`unshare` here costs bwrap nothing.

Compiled to BPF at image build time with libseccomp's Python binding
(`python3-seccomp`) on the image's own architecture, and versioned with the
service (`PROFILE_VERSION`):

    python -m sprintbaton.sandbox.server.seccomp --out /etc/sprintbaton/seccomp.bpf
"""

from __future__ import annotations

import argparse
import errno
import sys

PROFILE_VERSION = "1"

DENIED_SYSCALLS = (
    # process inspection / injection
    "ptrace", "process_vm_readv", "process_vm_writev", "kcmp",
    # mounts and namespaces
    "mount", "umount2", "pivot_root", "unshare", "setns", "mount_setattr",
    "fsopen", "fsconfig", "fsmount", "fspick", "move_mount", "open_tree",
    # kernel keyrings
    "keyctl", "add_key", "request_key",
    # kernel attack surface
    "bpf", "userfaultfd", "perf_event_open", "io_uring_setup",
    "io_uring_enter", "io_uring_register", "lookup_dcookie",
    "open_by_handle_at", "name_to_handle_at",
    # the machine itself
    "kexec_load", "kexec_file_load", "init_module", "finit_module",
    "delete_module", "reboot", "swapon", "swapoff", "acct", "quotactl",
    "syslog", "iopl", "ioperm", "vhangup", "settimeofday", "clock_settime",
    "adjtimex", "clock_adjtime",
)

# clone(2) flags that create namespaces — denied on clone itself, since a
# namespace is exactly what an escape needs a fresh one of.
CLONE_NAMESPACE_FLAGS = {
    "CLONE_NEWNS": 0x00020000, "CLONE_NEWCGROUP": 0x02000000,
    "CLONE_NEWUTS": 0x04000000, "CLONE_NEWIPC": 0x08000000,
    "CLONE_NEWUSER": 0x10000000, "CLONE_NEWPID": 0x20000000,
    "CLONE_NEWNET": 0x40000000,
}


def build_filter():  # pragma: no cover - needs libseccomp; exercised at image build
    import seccomp  # libseccomp's binding: apt install python3-seccomp

    f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    for name in DENIED_SYSCALLS:
        try:
            f.add_rule(seccomp.ERRNO(errno.EPERM), name)
        except (RuntimeError, ValueError):
            pass  # not a syscall on this architecture
    for flag in CLONE_NAMESPACE_FLAGS.values():
        f.add_rule(seccomp.ERRNO(errno.EPERM), "clone",
                   seccomp.Arg(0, seccomp.MASKED_EQ, flag, flag))
    # clone3 passes its flags in a struct seccomp cannot inspect: refuse it
    # with ENOSYS so libc falls back to clone(2), which the rules above cover.
    try:
        f.add_rule(seccomp.ERRNO(errno.ENOSYS), "clone3")
    except (RuntimeError, ValueError):
        pass
    return f


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.out, "wb") as out:
        build_filter().export_bpf(out)
    print(f"seccomp profile v{PROFILE_VERSION} written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
