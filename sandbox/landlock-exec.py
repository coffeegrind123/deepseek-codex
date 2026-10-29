#!/usr/bin/env python3
"""Run a command tree under a Landlock cage: read anywhere, write only where allowed.

Codex's own Linux sandbox is bubblewrap, which needs user namespaces; Docker's default
seccomp profile denies those (unshare -> EPERM), so every sandboxed command fails.
Landlock needs no namespaces: it is a per-process LSM policy inherited by every child,
so applying it here restricts Codex, its exec-server, every subagent and every shell
command they run. Requires Linux >= 5.13 with Landlock enabled (ABI 4+ for TCP rules).

    landlock-exec.py --rw DIR [--rw DIR ...] [--tcp-connect PORT ...] [--net open] -- CMD ARGS...

Everything else stays readable and executable (the toolchain lives outside the cage).
"""
import argparse
import ctypes
import ctypes.util
import os
import sys

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2
PR_SET_NO_NEW_PRIVS = 38

# include/uapi/linux/landlock.h access rights, with the ABI that introduced each one.
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13      # ABI 2
FS_TRUNCATE = 1 << 14   # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5
NET_BIND_TCP = 1 << 0   # ABI 4
NET_CONNECT_TCP = 1 << 1

FS_ABI1 = (FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_READ_DIR | FS_REMOVE_DIR
           | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG | FS_MAKE_SOCK
           | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM)
FS_READ_ONLY = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
# Character devices Codex and shells need to write: /dev/null, /dev/tty, /dev/pts/*.
DEV_WRITE = FS_WRITE_FILE | FS_IOCTL_DEV
DEV_DIR = '/dev'


class RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64),
                ('handled_access_net', ctypes.c_uint64),
                ('scoped', ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


class NetPortAttr(ctypes.Structure):
    _fields_ = [('allowed_access', ctypes.c_uint64), ('port', ctypes.c_uint64)]


libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
libc.syscall.restype = ctypes.c_long


def fail(message):
    sys.stderr.write(f'landlock-exec: {message}\n')
    sys.exit(125)


def syscall(number, *args):
    result = libc.syscall(number, *args)
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def landlock_abi():
    try:
        return syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION)
    except OSError as err:
        fail(f'Landlock unavailable ({err.strerror}); kernel must have CONFIG_SECURITY_LANDLOCK '
             'and the seccomp profile must allow landlock_* syscalls')


def fs_rights_for(abi):
    rights = FS_ABI1
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


def add_path_rule(ruleset_fd, path, access):
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attr = PathBeneathAttr(allowed_access=access, parent_fd=fd)
        syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(attr), 0)
    finally:
        os.close(fd)


def add_net_rule(ruleset_fd, port, access):
    attr = NetPortAttr(allowed_access=access, port=port)
    syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_NET_PORT, ctypes.byref(attr), 0)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rw', action='append', default=[], metavar='DIR',
                        help='directory tree that stays fully writable (repeatable)')
    parser.add_argument('--tcp-connect', action='append', type=int, default=[], metavar='PORT',
                        help='TCP port outbound connections may target (repeatable); implies restricted net')
    parser.add_argument('--tcp-bind', action='append', type=int, default=[], metavar='PORT',
                        help='TCP port that may be bound (repeatable)')
    parser.add_argument('--net', choices=['open', 'restricted'], default='restricted',
                        help='open leaves TCP alone; restricted allows only the listed ports (default)')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='-- CMD ARGS...')
    args = parser.parse_args()
    if args.command and args.command[0] == '--':
        args.command = args.command[1:]
    if not args.command:
        parser.error('no command given (use: -- CMD ARGS...)')
    if not args.rw:
        parser.error('at least one --rw DIR is required')
    return args


def main():
    args = parse_args()
    abi = landlock_abi()
    fs_rights = fs_rights_for(abi)
    net_rights = 0
    if args.net == 'restricted':
        if abi < 4:
            fail(f'Landlock ABI {abi} has no TCP rules; use --net open or a kernel >= 6.7')
        net_rights = NET_BIND_TCP | NET_CONNECT_TCP

    attr = RulesetAttr(handled_access_fs=fs_rights, handled_access_net=net_rights, scoped=0)
    # Older kernels reject a struct longer than they know; pass only the fields the ABI has.
    attr_size = ctypes.sizeof(RulesetAttr) if abi >= 6 else (16 if abi >= 4 else 8)
    ruleset_fd = syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), attr_size, 0)

    add_path_rule(ruleset_fd, '/', FS_READ_ONLY)
    if os.path.isdir(DEV_DIR):
        add_path_rule(ruleset_fd, DEV_DIR, DEV_WRITE & fs_rights)
    for directory in args.rw:
        real = os.path.realpath(directory)
        if not os.path.isdir(real):
            fail(f'--rw {directory}: not a directory')
        add_path_rule(ruleset_fd, real, fs_rights)
    if net_rights:
        for port in args.tcp_connect:
            add_net_rule(ruleset_fd, port, NET_CONNECT_TCP)
        for port in args.tcp_bind:
            add_net_rule(ruleset_fd, port, NET_BIND_TCP)

    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        fail('prctl(PR_SET_NO_NEW_PRIVS) failed')
    syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    os.close(ruleset_fd)

    if args.verbose:
        sys.stderr.write(f'landlock-exec: abi={abi} rw={[os.path.realpath(d) for d in args.rw]} '
                         f'net={args.net} connect={args.tcp_connect} bind={args.tcp_bind}\n')
    os.environ['CODEX_LANDLOCK_CAGE'] = ':'.join(os.path.realpath(d) for d in args.rw)
    try:
        os.execvp(args.command[0], args.command)
    except OSError as err:
        fail(f'exec {args.command[0]}: {err.strerror}')


if __name__ == '__main__':
    main()
