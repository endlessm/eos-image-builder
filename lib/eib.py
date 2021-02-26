# -*- mode: Python; coding: utf-8 -*-

# Endless image builder library
#
# Copyright (C) 2015  Endless Mobile, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# NOTE - This module is used from the host system to setup the build, so
# any resources used here (python modules, executed programs) must be
# available there. Use a separate module and make sure the components
# are in the buildroot if the utility is only inside the build.

from argparse import ArgumentParser
import configparser
from collections import Counter, OrderedDict
import errno
import fcntl
import fnmatch
import glob
import itertools
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

CACHEDIR = '/var/cache/eos-image-builder'
BUILDDIR = '/var/tmp/eos-image-builder'
SYSCONFDIR = '/etc/eos-image-builder'
LOCKFILE = '/var/lock/eos-image-builder.lock'
LOCKTIMEOUT = 60

SUPPORTED_ARCHES = [
    'i386',
    'amd64',
    'arm64',
    'armhf'
]

# Python normally catches SIGINT and converts it to the
# KeyboardInterrupt exception. Unfortunately, if some code is blocking
# the main thread (e.g, OSTree.Repo.pull), the exception can't be
# delivered and the image builder won't stop on ^C.
#
# Set the signal handler back to the default (Term) so the image builder
# dies. It's not intended to be run interactively where
# KeyboardInterrupt would be useful, and any code that needs this
# behavior can restore python's default signal handler.
DEFAULT_SIGINT_HANDLER = signal.signal(signal.SIGINT, signal.SIG_DFL)

# Constants for inode attributes. The ioctl value differs on 32 and 64
# bit systems. To check the values, compile and run the following:
#
# #include <stdio.h>
# #include <linux/fs.h>
# int main(void)
# {
#   printf("FS_IMMUTABLE_FL=%#010x\n", FS_IMMUTABLE_FL);
#   printf("FS_IOC_GETFLAGS=%#0lx\n", (unsigned long)FS_IOC_GETFLAGS);
#   printf("FS_IOC_SETFLAGS=%#0lx\n", (unsigned long)FS_IOC_SETFLAGS);
#   return 0;
# }
FS_IMMUTABLE_FL = 0x00000010
if sys.maxsize < (1 << 32):
    # 32 bit system
    FS_IOC_GETFLAGS = 0x80046601
    FS_IOC_SETFLAGS = 0x40046602
else:
    # 64 bit system
    FS_IOC_GETFLAGS = 0x80086601
    FS_IOC_SETFLAGS = 0x40086602


class ImageBuildError(Exception):
    """Errors from the image builder"""
    def __init__(self, *args):
        self.msg = ' '.join(map(str, args))

    def __str__(self):
        return str(self.msg)


class ImageConfigParser(configparser.ConfigParser):
    """Configuration parser for the image builder. This uses configparser's
    ExtendedInterpolation to expand values like variables."""

    BUILD_SECTION = 'build'

    # Config options that will be merged together from multiple
    # $prefix_add* and $prefix_del* options. This is a list of (section,
    # prefix) tuples. The section can be a glob pattern.
    #
    # FIXME: This needs to be managed from the config itself for
    # flexibility.
    MERGED_OPTIONS = [
        ('buildroot', 'mounts'),
        ('buildroot', 'packages'),
        ('check', 'hooks'),
        ('content', 'hooks'),
        ('error', 'hooks'),
        ('flatpak', 'locales'),
        ('flatpak-remote-*', 'apps'),
        ('flatpak-remote-*', 'runtimes'),
        ('flatpak-remote-*', 'nosplit_apps'),
        ('flatpak-remote-*', 'nosplit_runtimes'),
        ('flatpak-remote-*', 'exclude'),
        ('flatpak-remote-*', 'allow_extra_data'),
        ('image', 'branding_subst_vars'),
        ('image', 'hooks'),
        ('image', 'icon_grid'),
        ('image', 'settings'),
        ('image', 'settings_locks'),
        ('manifest', 'hooks'),
        ('publish', 'hooks'),
        ('split', 'hooks'),
    ]

    def __init__(self, *args, **kwargs):
        kwargs['interpolation'] = configparser.ExtendedInterpolation()
        super().__init__(*args, **kwargs)

        # Always add the build section
        self.add_section(self.BUILD_SECTION)

        self.namespaces = set()

    def read_config_file(self, path, namespace):
        """Read a single file into the configuration

        The file does not need to exist. Returns True if the file was
        read and False otherwise. The namespace parameter is used as a
        suffix for merged options when one does not exist in the config
        file and must be unique.
        """
        # Ensure the namespace is set and unique.
        if not namespace:
            raise ImageBuildError('namespace must be a non-empty string')
        if namespace in self.namespaces:
            raise ImageBuildError('namespace', namespace, 'is already used')
        self.namespaces.add(namespace)

        # Load this file into a normal ConfigParser instance without
        # interpolation so that it can adjusted before merging it with
        # the full configuration.
        path = os.fspath(path)
        logger.debug('Considering config file %s', path)
        raw_config = configparser.ConfigParser(interpolation=None)
        if not raw_config.read(path, encoding='utf-8'):
            return False

        # Include a leading _ in the namespace suffix if needed.
        suffix = namespace
        if suffix[0] != '_':
            suffix = '_' + suffix

        for pattern, opt in self.MERGED_OPTIONS:
            add_opt = opt + '_add'
            del_opt = opt + '_del'

            for sect in fnmatch.filter(raw_config.sections(), pattern):
                section = raw_config[sect]

                if add_opt in section:
                    namespace_add_opt = add_opt + suffix
                    logger.debug('Renaming %s option %s to %s', sect, add_opt,
                                 namespace_add_opt)
                    section[namespace_add_opt] = section[add_opt]
                    del section[add_opt]

                if del_opt in section:
                    namespace_del_opt = del_opt + suffix
                    logger.debug('Renaming %s option %s to %s', sect, del_opt,
                                 namespace_del_opt)
                    section[namespace_del_opt] = section[del_opt]
                    del section[del_opt]

        # Merge this configuration.
        self.read_dict(raw_config)

        return True

    def setboolean(self, section, option, value):
        """Convenience method to store boolean's in shell style
        true/false
        """
        assert(isinstance(value, bool))
        if value:
            value = 'true'
        else:
            value = 'false'
        self.set(section, option, value)

    def merge(self):
        """Merge the options in the configuration"""
        unmerged_options = []
        for section, option in self.MERGED_OPTIONS:
            unmerged_options += self._merge_option(section, option)

        # Delete the unmerged options
        for section, option in unmerged_options:
            logger.debug('Deleting unmerged option %s %s', section, option)
            del self[section][option]

    def _merge_option(self, section_pattern, option):
        """Merge multiple options named like <option>_add* and <option>_del*.
        The original unmerged options are then deleted. If an option
        named <prefix> already exists, it is not changed.

        The section can be a glob pattern to merge options in similarly
        named sections.

        This function is a generator yielding unmerged (section, option)
        tuples.
        """
        for section in fnmatch.filter(self.sections(), section_pattern):
            sect = self[section]

            add_opts = fnmatch.filter(sect.keys(), option + '_add*')
            del_opts = fnmatch.filter(sect.keys(), option + '_del*')

            # If the option already exists, it overrides the unmerged
            # variants
            if option in sect:
                logger.debug('Keeping merged option %s %s', section, option)
                for opt in add_opts + del_opts:
                    logger.debug('Ignoring unmerged option %s %s', section,
                                 opt)
                    yield (section, opt)
            else:
                add_vals = Counter()
                for opt in add_opts:
                    logger.debug('Adding %s %s values from %s', section,
                                 option, opt)
                    add_vals.update(sect[opt].split())
                    yield (section, opt)

                del_vals = Counter()
                for opt in del_opts:
                    logger.debug('Removing %s %s values from %s', section,
                                 option, opt)
                    del_vals.update(sect[opt].split())
                    yield (section, opt)

                # Set the option to the difference of the counters.
                # Merge the values together with newlines like they were
                # in the original configuration.
                vals = add_vals - del_vals
                sect[option] = '\n'.join(sorted(vals.keys()))

    def copy(self):
        """Create a new instance from this one"""
        # Build a dictionary with raw values rather than passing this
        # instance directly into the new instance's read_dict method.
        data = OrderedDict()
        for sect in self.sections():
            data[sect] = OrderedDict(self.items(sect, raw=True))
        new_config = ImageConfigParser()
        new_config.read_dict(data)
        return new_config

    def getenv(self, section, option):
        """Get config value as variable in EIB namespace

        The variable name will be EIB_<SECTION>_<OPTION> with the
        section and option names uppercased. Shell-incompatible
        characters are converted to underscores.

        As a special case, options from the build section do not have
        the section name in the environment variable. For instance,
        build:product will become EIB_PRODUCT.

        Returns a tuple of name and value.
        """
        value = self[section][option]

        # Convert boolean values to true/false to be handled easily in
        # shell
        if value.lower() in ('true', 'false'):
            value = value.lower()

        # Construct the variable name
        var = 'EIB_'
        if section != self.BUILD_SECTION:
            var += section.upper() + '_'
        var += option.upper()

        # Per POSIX, environment variable names compatible with shells
        # only contain upper case letters, digits and underscores.
        # Convert anything else to an underscore.
        var = re.sub(r'[^A-Z0-9_]', '_', var)

        return var, value

    def get_environment(self):
        """Get all environment variables for configuration"""
        return dict([
            self.getenv(sect, opt)
            for sect in self.sections()
            for opt in self.options(sect)
        ])


def recreate_dir(path):
    """Delete and recreate a directory"""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def add_cli_options(argparser):
    """Add command line options for eos-image-builder. This allows the
    settings to be shared between eos-image-builder and run-build.
    """
    assert(isinstance(argparser, ArgumentParser))

    def add_argument(*args, **kwargs):
        kwargs['help'] += ' (default: {})'.format(kwargs['default'])
        return argparser.add_argument(*args, **kwargs)

    argparser.add_argument('-d', '--localdir', help='local settings directory')

    add_argument('-p', '--product', default='eos',
                 help='product to build')
    argparser.add_argument('-a', '--arch', choices=SUPPORTED_ARCHES,
                           help='architecture to build '
                                '(default: host architecture)')
    argparser.add_argument('--platform',
                           help='platform to build (default: depends on arch)')
    add_argument('-P', '--personality', default='base',
                 help='personality to build')

    info = argparser.add_argument_group(
        'informational modes',
        description='These options show information about the image '
                    'configuration, without actually building it',
    ).add_mutually_exclusive_group()
    info.add_argument('--show-config', action='store_true',
                      help='show configuration and exit')
    info.add_argument('--show-apps', action='store_true',
                      help='show apps which will be added to the image, '
                           'including their approximate compressed size')

    show_apps = argparser.add_argument_group('options for --show-apps')
    show_apps.add_argument(
        '--split', action='store_true',
        help='list the apps which will be included in a split image')
    show_apps.add_argument(
        '--trim', metavar='EXCESS', type=int, default=0,
        help='propose which apps to remove to save approximately EXCESS bytes '
             'in the compressed image')
    show_apps.add_argument(
        '--group-by', metavar='GROUPING',
        choices=('nature', 'runtime'), default='runtime',
        help='group apps by their "nature" (locale-specific, generic or '
             'runtime) or by the "runtime" they use (default)')

    argparser.add_argument('-n', '--dry-run', action='store_true',
                           help="don't publish images")
    argparser.add_argument('--debug', action='store_true',
                           help="enable slightly more verbose logging")
    argparser.add_argument('--use-production', action='store_true',
                           help="use production ostree/flatpak repos rather than staging (deprecated)")
    argparser.add_argument('--use-production-ostree', action='store_true',
                           help="use production ostree repos rather than staging")

    add_argument('--lock-timeout', type=int, default=LOCKTIMEOUT,
                 help='time in seconds to acquire lock before '
                      'exiting')
    add_argument('branch', nargs='?', default='master',
                 help='branch to build')


def setup_logging():
    log_format = '+ %(asctime)s %(levelname)s %(name)s: %(message)s'
    date_format = '%H:%M:%S'

    # The log level is controlled by an environment variable rather than a
    # function parameter so it can be inherited by hooks.
    if os.environ.get('EIB_DEBUG', '') == '1':
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format=log_format, datefmt=date_format)


def get_keyring(config):
    """Get the path to the temporary GPG keyring

    If it doesn't exist, it will be created.
    """
    keyring = config['build']['keyring']

    if not os.path.isfile(keyring):
        logger.info('Creating temporary GPG keyring %s', keyring)

        keyspaths = [os.path.join(config['build']['datadir'], 'keys')]
        if 'localdatadir' in config['build']:
            keyspaths.append(os.path.join(config['build']['localdatadir'],
                                          'keys'))

        keysdirs = list(filter(os.path.isdir, keyspaths))
        if len(keysdirs) == 0:
            raise ImageBuildError('No gpg keys directories at',
                                  ' or '.join(keyspaths))

        keys = list(itertools.chain.from_iterable(
            [glob.iglob(os.path.join(d, '*.asc')) for d in keysdirs]
        ))
        if len(keys) == 0:
            raise ImageBuildError('No gpg keys in', ' or '.join(keysdirs))

        # Use a temporary gpg homedir
        with tempfile.TemporaryDirectory(dir=config['build']['tmpdir'],
                                         prefix='eib-keyring') as homedir:
            # Import the keys
            for key in keys:
                logger.info('Importing GPG key %s', key)
                subprocess.check_call(['gpg', '--batch', '--quiet',
                                       '--homedir', homedir,
                                       '--import', key])

            # Export all the keys as a normal PGP stream since newer
            # gnupg imports to a keybox.
            subprocess.check_call(['gpg', '--batch', '--quiet',
                                   '--homedir', homedir,
                                   '--export', '--output', keyring])

    return keyring


def disk_usage(path):
    """Recursively gather disk usage in bytes for path"""
    total = os.stat(path, follow_symlinks=False).st_size
    for root, dirs, files in os.walk(path):
        total += sum([os.stat(os.path.join(root, name),
                              follow_symlinks=False).st_size
                      for name in dirs + files])
    return total


def retry(func, *args, max_retries=3, timeout=1, **kwargs):
    """Retry a function in case of intermittent errors"""
    # A no-op if the hook has already called this
    setup_logging()

    retry = 0
    while True:
        try:
            return func(*args, **kwargs)
        except:
            retry += 1
            if retry > max_retries:
                logger.error('Failed %d retries; giving up', max_retries)
                raise

            # Show the traceback so the error isn't hidden
            logger.warning('Retrying attempt %d', retry, exc_info=True)
            time.sleep(timeout)


def get_config(path=None):
    """Read and parse the full merged config file

    Returns an ImageConfigParser instance populated with the full merged
    config file. This can be used by hooks or helpers in preference to
    scraping the EIB_* environment variables. If path is not provided,
    it is looked for in the EIB_TMPFULLCONFIG environment variable.
    """
    if path is None:
        path = os.getenv('EIB_TMPFULLCONFIG')
        if not path:
            raise ImageBuildError(
                'Path to config file not set in EIB_TMPFULLCONFIG')
    if not os.path.exists(path):
        raise ImageBuildError('No config file found at', path)

    config = ImageConfigParser()
    if not config.read(path, encoding='utf-8'):
        raise ImageBuildError('No configuration read from', path)

    return config


def get_manifest(path=None):
    """Read and parse the full merged manifest file

    This will only work if called after the eib_manifest stage has completed.

    Returns a Python object containing the merged manifest JSON data. If path
    is not provided, it is looked for as
    ${EIB_OUTDIR}/${EIB_OUTVERSION}.manifest.json.
    """
    if path is None:
        outversion = os.getenv('EIB_OUTVERSION')
        path = os.path.join(os.getenv('EIB_OUTDIR'),
                            outversion + '.manifest.json')
        if not path:
            raise ImageBuildError(
                'Path to manifest file not set in EIB_OUTDIR')
    if not os.path.exists(path):
        raise ImageBuildError('No manifest file found at', path)

    with open(path) as f:
        return json.load(f)


def signal_root_processes(root, sig):
    """Send signal sig to all processes in root path

    Walks the list of processes and check if /proc/$pid/root is within
    root. If so, it's sent signal.
    """
    killed_procs = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue

        # Try to get the proc's root, but ignore errors if the process
        # went away
        try:
            pid_root = os.readlink(os.path.join('/proc', pid, 'root'))
        except FileNotFoundError:
            continue

        # Check if the pid's root is the chroot or a subdirectory (a
        # process that did a subsequent chroot)
        if pid_root == root or pid_root.startswith(root + '/'):
            killed_procs.append(pid)

            # Try to read the exe file, but in some cases (kernel
            # thread), it may not exist
            try:
                pid_exe = os.readlink(os.path.join('/proc', pid, 'exe'))
            except:
                pid_exe = ''

            # Kill it
            logger.info('Killing pid %s %s with signal %s', pid, pid_exe,
                        sig)
            try:
                os.kill(int(pid), sig)
            except ProcessLookupError:
                logger.debug('Process %s already exited', pid)

    return killed_procs


def kill_root_processes(root):
    """Kill all processes running under root path"""
    # Kill once with SIGTERM, then with SIGKILL. If any processes were
    # killed, sleep for a second to allow them to cleanup resources.
    if len(signal_root_processes(root, signal.SIGTERM)) > 0:
        time.sleep(1)
    if len(signal_root_processes(root, signal.SIGKILL)) > 0:
        time.sleep(1)


def loop_has_partitions(loop):
    """Get a list of partitions for the loop device"""
    loop_part_pattern = os.path.join('/sys/block', loop, loop + 'p*')
    loop_part_regex = re.compile(r'/{}p\d+$'.format(loop))
    loop_parts = [path for path in glob.iglob(loop_part_pattern)
                  if loop_part_regex.search(path)]
    return len(loop_parts) > 0


def udevadm_settle():
    """Run udevadm settle to wait for device events to be processed

    Tell udevadm to ignore that we're in a chroot since we expect the
    udev control socket to be bind mounted into it.
    """
    # If settle can't connect to the /run/udev/control socket, it will
    # simply return without an error. Print an error in that case but
    # carry on since skipping settle may not be fatal.
    if not os.path.exists('/run/udev/control'):
        logger.error('/run/udev/control does not exist when calling '
                     '"udevadm settle"')
        return

    env = os.environ.copy()
    env['SYSTEMD_IGNORE_CHROOT'] = '1'
    subprocess.check_call(('udevadm', 'settle'), env=env)


def delete_root_loops(root):
    """Delete all loop devices with backing files in root path

    Look for any active loop devices that have a backing file within
    root and delete them. If the loop device has any active partitions,
    they'll be removed first.
    """
    root_loops = []
    # The contents of backing_file end in a newline and can have a
    # trailing " (deleted)" if the backing file was deleted. Both will
    # be stripped assuming we don't have an actual file ending with
    # (deleted).
    backing_file_regex = re.compile(r'( \(deleted\))?\n?$')
    loop_regex = re.compile(r'/loop\d+$')
    all_loop_paths = [path for path in glob.iglob('/sys/block/loop*')
                      if loop_regex.search(path)]
    for loop_path in all_loop_paths:
        backing_path = os.path.join(loop_path, 'loop/backing_file')
        if not os.path.exists(backing_path):
            continue
        with open(backing_path) as f:
            backing_file = backing_file_regex.sub('', f.read())
        if backing_file.startswith(root + '/'):
            loop_name = os.path.basename(loop_path)
            root_loops.append(loop_name)

    for loop in root_loops:
        loop_dev = os.path.join('/dev', loop)

        if loop_has_partitions(loop):
            logger.info('Deleting loop partitions for %s', loop_dev)
            subprocess.check_call(('partx', '-d', loop_dev))

            # Try to block until the partition devices are removed
            udevadm_settle()

        logger.info('Deleting loop %s', loop_dev)
        retry(subprocess.check_call, ('losetup', '-d', loop_dev))


def unmount_root_filesystems(root):
    """Unmount all filesystems in root path

    Finds all filesystems mounted within root and unmounts them.
    """
    # Re-read the mount table after every unmount in case there
    # are aliased mounts
    while True:
        path = None
        with open('/proc/self/mountinfo') as mountf:
            mounts = mountf.readlines()

        # Operate on the mounts backwards to unmount submounts first
        for line in reversed(mounts):
            mountdir = line.split()[4]
            # Search for mount on root or that begin with root/. The
            # trailing / is added to ensure that only paths below $dir
            # are matched and not $dir-backup or anything else that
            # begins with the same characters.
            if mountdir == root or mountdir.startswith(root + '/'):
                path = mountdir
                break

        if path is None:
            # No more paths to unmount
            break

        # Before unmounting this filesystem, delete any loop devices
        # with backing files in it. Since the unmounting is happening in
        # reverse, we can hopefully assume that any mounts of the loop
        # would have happened at a later mount under the top root path
        # and therefore have already been unmounted.
        delete_root_loops(path)

        logger.info('Unmounting %s', path)
        subprocess.check_call(['umount', path])


def cleanup_root(root):
    """Cleanup root for deletion

    Cleans up processes, mounts and loops for the given root path. After
    this the root should be able to be deleted.
    """
    logger.info('Killing processes in %s', root)
    kill_root_processes(root)

    logger.info('Unmounting filesystems in %s', root)
    unmount_root_filesystems(root)


def mutable_path(path):
    """
    Make the inode for path mutable

    This is equivalent to `chattr -i` except that it catches errors when
    the inode attributes are not supported for a specific filesystem. In
    particular, this will ignore failures getting and changing attributes
    for a directory on overlayfs in a docker container.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        buf = fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack('i', 0))
        attr = struct.unpack('i', buf)[0]
        if (attr & FS_IMMUTABLE_FL):
            # Clear the immutable bit
            attr &= ~(FS_IMMUTABLE_FL)
        else:
            # Already mutable, nothing to do
            logger.debug('Path "%s" already mutable', path)
            return
        buf = struct.pack('i', attr)
        fcntl.ioctl(fd, FS_IOC_SETFLAGS, buf)
    except IOError as err:
        # When inode attributes aren't supported, the error will be
        # ENOTTY (Inappropriate ioctl) or ENOTSUP (Operation not
        # supported)
        if err.errno in (errno.ENOTTY, errno.ENOTSUP):
            logger.info('Inode attributes for "%s" not supported', path)
        else:
            raise
    finally:
        os.close(fd)
