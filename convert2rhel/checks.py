# -*- coding: utf-8 -*-
#
# Copyright(C) 2016 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import itertools
import logging
import os
import re
import subprocess

from convert2rhel.systeminfo import system_info
from convert2rhel.utils import run_subprocess


logger = logging.getLogger(__name__)

KERNEL_REPO_RE = re.compile("^.+:(?P<version>.+).el.+$")
KERNEL_REPO_VER_SPLIT_RE = re.compile("\W+")
LINK_KMODS_RH_POLICY = "https://access.redhat.com/third-party-software-support"


def perform_pre_checks():
    """Early checks after system facts should be added here."""
    check_uefi()
    check_tainted_kmods()


def check_uefi():
    """Inhibit the conversion when UEFI detected."""
    logger.task("Prepare: Checking the firmware interface type")
    if os.path.exists("/sys/firmware/efi"):
        # NOTE(pstodulk): the check doesn't have to be valid for hybrid boot
        # (e.g. AWS, Azure, OSP, ..)
        logger.critical(
            "Conversion of UEFI systems is currently not supported, see"
            " https://bugzilla.redhat.com/show_bug.cgi?id=1898314"
            " for more information."
        )
    logger.debug("Converting BIOS system")


def check_tainted_kmods():
    """Stop the conversion when a loaded tainted kernel module is detected.

    Tainted kmods ends with (...) in /proc/modules, for example:
        multipath 20480 0 - Live 0x0000000000000000
        linear 20480 0 - Live 0x0000000000000000
        system76_io 16384 0 - Live 0x0000000000000000 (OE)  <<<<<< Tainted
        system76_acpi 16384 0 - Live 0x0000000000000000 (OE) <<<<< Tainted
    """
    unsigned_modules, _ = run_subprocess("grep '(' /proc/modules")
    module_names = "\n  ".join(
        [mod.split(" ")[0] for mod in unsigned_modules.splitlines()]
    )
    if unsigned_modules:
        logger.critical(
            "Tainted kernel module(s) detected. "
            "Third-party components are not supported per our "
            "software support policy\n%s\n\n"
            "Uninstall or disable the following module(s) and run convert2rhel "
            "again to continue with the conversion:\n  %s",
            LINK_KMODS_RH_POLICY,
            module_names,
        )


def perform_pre_ponr_checks():
    """Late checks before ponr should be added here."""
    ensure_compatibility_of_kmods()


def ensure_compatibility_of_kmods():
    """Ensure if the host kernel modules are compatible with RHEL."""
    host_kmods = get_installed_kmods()
    rhel_supported_kmods = get_rhel_supported_kmods()
    if is_unsupported_kmod_installed(host_kmods, rhel_supported_kmods):
        kernel_version = run_subprocess("uname -r")[0].rstrip("\n")
        not_supported_kmods = "\n".join(
            map(
                lambda kmod: "/lib/modules/{kver}/{kmod}".format(
                    kver=kernel_version, kmod=kmod
                ),
                host_kmods - rhel_supported_kmods,
            )
        )
        # TODO logger.critical("message %s, %s", "what should be under s")
        #  doesn't work. We have `%s` as output instead. Make it work
        logger.critical(
            (
                "The following kernel modules are not "
                "supported in RHEL:\n{kmods}\n"
                "Uninstall or disable them and run convert2rhel "
                "again to continue with the conversion."
            ).format(kmods=not_supported_kmods)
        )
    else:
        logger.debug("Kernel modules are compatible.")


def get_installed_kmods():
    """Get a set of kernel modules.

    Each module we cut part of the path until the kernel release
    (i.e. /lib/modules/5.8.0-7642-generic/kernel/lib/a.ko.xz ->
    kernel/lib/a.ko.xz) in order to be able to compare with RHEL
    kernel modules in case of different kernel release
    """
    try:
        kernel_version, exit_code = run_subprocess("uname -r")
        assert exit_code == 0
        kmod_str, exit_code = run_subprocess(
            'find /lib/modules/{kver} -name "*.ko*" -type f'.format(
                kver=kernel_version.rstrip("\n")
            ),
            print_output=False,
        )
        assert exit_code == 0
        assert kmod_str
    except (subprocess.CalledProcessError, AssertionError):
        logger.critical("Can't get list of kernel modules.")
    else:
        return set(
            _get_kmod_comparison_key(path)
            for path in kmod_str.rstrip("\n").split()
        )


def _get_kmod_comparison_key(path):
    """Create a comparison key from the kernel module abs path.

    Converts /lib/modules/5.8.0-7642-generic/kernel/lib/a.ko.xz ->
    kernel/lib/a.ko.xz

    Why:
        The standard kernel modules are located under
        /lib/modules/{some kernel release}/.
        If we want to make sure that the kernel package is present
        on RHEL, we need to compare the full path, but because kernel release
        might be different, we compare the relative paths after kernel release.
    """
    return "/".join(path.split("/")[4:])


def get_rhel_supported_kmods():
    """Return set of target RHEL supported kernel modules."""
    repoquery_repoids_args = " ".join(
        "--repoid " + repoid for repoid in system_info.get_enabled_rhel_repos()
    )
    # Without the release package installed, dnf can't determine the modularity
    #   platform ID.
    setopt_arg = (
        "--setopt=module_platform_id=platform:el8"
        if system_info.version.major == 8
        else ""
    )
    # get output of a command to get all packages which are the source
    # of kmods
    kmod_pkgs_str, _ = run_subprocess(
        (
            "repoquery "
            "--releasever={releasever} "
            "{setopt_arg} "
            "{repoids_args} "
            "-f /lib/modules/*.ko*"
        ).format(
            releasever=system_info.releasever,
            setopt_arg=setopt_arg,
            repoids_args=repoquery_repoids_args,
        ),
        print_output=False,
    )
    # from these packages we select only the latest one
    kmod_pkgs = get_most_recent_unique_kernel_pkgs(
        kmod_pkgs_str.rstrip("\n").split()
    )
    # querying obtained packages for files they produces
    rhel_kmods_str, _ = run_subprocess(
        (
            "repoquery "
            "--releasever={releasever} "
            "{setopt_arg} "
            "{repoids_args} "
            "-l {pkgs}"
        ).format(
            releasever=system_info.releasever,
            setopt_arg=setopt_arg,
            repoids_args=repoquery_repoids_args,
            pkgs=" ".join(kmod_pkgs),
        ),
        print_output=False,
    )
    return get_rhel_kmods_keys(rhel_kmods_str)


def get_most_recent_unique_kernel_pkgs(pkgs):
    """Return the most recent versions of all kernel packages.

    When we scan kernel modules provided by kernel packages
    it is expensive to check each kernel pkg. Since each new
    kernel pkg do not deprecate kernel modules we only select
    the most recent ones.

    All RHEL kmods packages starts with kernel* or kmod*

    For example, we have the following packages list:
        kernel-core-0:4.18.0-240.10.1.el8_3.x86_64
        kernel-core-0:4.19.0-240.10.1.el8_3.x86_64
        kmod-debug-core-0:4.18.0-240.10.1.el8_3.x86_64
        kmod-debug-core-0:4.18.0-245.10.1.el8_3.x86_64
    ==> (output of this function will be)
        kernel-core-0:4.19.0-240.10.1.el8_3.x86_64
        kmod-debug-core-0:4.18.0-245.10.1.el8_3.x86_64

    _repos_version_key extract the version of a package
        into the tuple, i.e.
        kernel-core-0:4.18.0-240.10.1.el8_3.x86_64 ==>
        (4, 15, 0, 240, 10, 1)


    :type pkgs: Iterable[str]
    :type pkgs_groups:
        Iterator[
            Tuple[
                package_name_without_version,
                Iterator[package_name, ...],
                ...,
            ]
        ]
    """

    pkgs_groups = itertools.groupby(
        sorted(pkgs), lambda pkg_name: pkg_name.split(":")[0]
    )
    return (
        max(distinct_kernel_pkgs[1], key=_repos_version_key)
        for distinct_kernel_pkgs in pkgs_groups
        if distinct_kernel_pkgs[0].startswith(("kernel", "kmod"))
    )


def _repos_version_key(pkg_name):
    try:
        rpm_version = KERNEL_REPO_RE.search(pkg_name).group("version")
    except AttributeError:
        logger.critical(
            "Unexpected package:\n%s\n is a source of kernel modules.",
            pkg_name,
        )
    else:
        return tuple(
            map(
                _convert_to_int_or_zero,
                KERNEL_REPO_VER_SPLIT_RE.split(rpm_version),
            )
        )


def _convert_to_int_or_zero(s):
    try:
        return int(s)
    except ValueError:
        return 0


def get_rhel_kmods_keys(rhel_kmods_str):
    return set(
        _get_kmod_comparison_key(kmod_path)
        for kmod_path in filter(
            lambda path: path.endswith(("ko.xz", "ko")),
            rhel_kmods_str.rstrip("\n").split(),
        )
    )


def is_unsupported_kmod_installed(host_kmods, rhel_supported_kmods):
    """Return True if any of the installed kernel modules is not available in RHEL repositories."""
    return not host_kmods.issubset(rhel_supported_kmods)