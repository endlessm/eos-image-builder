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

from argparse import ArgumentParser, SUPPRESS
import configparser
from collections import Counter, OrderedDict
import fnmatch
import glob
import json
import logging
import os
import re
import shutil
import signal
import subprocess
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
    'armhf'
]

# Exit code indicating new build needed rather than error
CHECK_EXIT_BUILD_NEEDED = 90

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


class ImageBuildError(Exception):
    """Errors from the image builder"""
    def __init__(self, *args):
        self.msg = ' '.join(map(str, args))

    def __str__(self):
        return str(self.msg)


class ImageConfigParser(configparser.ConfigParser):
    """Configuration parser for the image builder. This uses configparser's
    ExtendedInterpolation to expand values like variables."""

    defaultsect = 'build'

    def __init__(self, *args, **kwargs):
        kwargs['interpolation'] = configparser.ExtendedInterpolation()
        kwargs['default_section'] = self.defaultsect
        super().__init__(*args, **kwargs)

    def items_no_default(self, section, raw=False):
        """Return the items in a section without including defaults"""
        # This is a nasty hack to overcome the behavior of the normal
        # items(). The default section needs to be merged in to resolve
        # the interpolation, but we only want the keys from the section
        # itself.
        d = self.defaults().copy()
        sect = self._sections[section]
        d.update(sect)
        if raw:
            def value_getter(option):
                return d[option]
        else:
            def value_getter(option):
                return self._interpolation.before_get(self,
                                                      section,
                                                      option,
                                                      d[option],
                                                      d)
        return [(option, value_getter(option)) for option in sect.keys()]

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

    def merge_option_prefix(self, section, prefix):
        """Merge multiple options named like <prefix>_add_* and
        <prefix>_del_*. The original options will be deleted.
        If an option named <prefix> already exists, it is not changed.

        The section can be a glob pattern to merge options in similarly
        named sections.
        """
        for sect_name in fnmatch.filter(self.sections(), section):
            sect = self[sect_name]
            add_opts = fnmatch.filter(sect.keys(), prefix + '_add_*')
            del_opts = fnmatch.filter(sect.keys(), prefix + '_del_*')

            # If the prefix doesn't exist, merge together the add and
            # del options and set it.
            if prefix not in sect:
                add_vals = Counter()
                for opt in add_opts:
                    add_vals.update(sect[opt].split())
                del_vals = Counter()
                for opt in del_opts:
                    del_vals.update(sect[opt].split())

                # Set the prefix to the difference of the counters.
                # Merge the values together with newlines like they were
                # in the original configuration.
                vals = add_vals - del_vals
                sect[prefix] = '\n'.join(sorted(vals.keys()))

            # Remove the add/del options to cleanup the section
            for opt in add_opts + del_opts:
                del sect[opt]

    def copy(self):
        """Create a new instance from this one"""
        # Construct a dict to feed into a new instance's read_dict
        data = OrderedDict()
        data[self.defaultsect] = OrderedDict(self.items(self.defaultsect,
                                                        raw=True))
        for sect in self.sections():
            data[sect] = OrderedDict(self.items_no_default(sect,
                                                           raw=True))

        # Construct the new instance
        new_config = ImageConfigParser()
        new_config.read_dict(data)
        return new_config


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

    add_argument('-p', '--product', default='eos',
                 help='product to build')
    argparser.add_argument('-a', '--arch', choices=SUPPORTED_ARCHES,
                           help='architecture to build '
                                '(default: host architecture)')
    argparser.add_argument('--platform',
                           help='platform to build (default: depends on arch)')
    add_argument('-P', '--personalities', default='base',
                 help='personalities to build')

    info = argparser.add_argument_group(
        'informational modes',
        description='These options show information about the image '
                    'configuration, without actually building it',
    ).add_mutually_exclusive_group()
    info.add_argument('--show-config', action='store_true',
                      help='show configuration and exit')
    info.add_argument('--show-apps', metavar='EXCESS',
                      nargs='?', type=int, const=0,
                      help='show apps which will be added to the image, '
                           'including their approximate compressed size. '
                           'If EXCESS is specified, propose which apps to '
                           'remove to save approximately EXCESS bytes in the '
                           'compressed image.')

    argparser.add_argument('-f', '--force', action='store_true',
                           help='run build even when no new assets found')
    argparser.add_argument('-n', '--dry-run', action='store_true',
                           help="don't publish images")
    argparser.add_argument('--debug', action='store_true',
                           help="enable slightly more verbose logging")
    argparser.add_argument('--use-production', action='store_true',
                           dest='use_production_ostree',
                           help="use production ostree repos rather than staging (deprecated)")
    argparser.add_argument('--use-production-apps', action='store_true',
                           help=SUPPRESS)
    argparser.add_argument('--use-production-ostree', action='store_true',
                           help="use production ostree repos rather than staging")
    argparser.add_argument('--build-from-tag',
                           help="use an eos-image-builder tag rather than the latest branch")
    argparser.add_argument('--checkout', action='store_true',
                           help='copy the git repo to the build directory')
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


def create_keyring(config):
    """Create the temporary GPG keyring if it doesn't exist"""
    keyring = config['build']['keyring']

    if not os.path.isfile(keyring):
        keysdir = config['build']['keysdir']
        if not os.path.isdir(keysdir):
            raise ImageBuildError('No gpg keys directory at', keysdir)

        keys = glob.glob(os.path.join(keysdir, '*.asc'))
        if len(keys) == 0:
            raise ImageBuildError('No gpg keys in', keysdir)

        # Use a temporary gpg homedir
        with tempfile.TemporaryDirectory(dir=config['build']['tmpdir'],
                                         prefix='eib-keyring') as homedir:
            # Import the keys
            for key in keys:
                subprocess.check_call(['gpg', '--batch', '--quiet',
                                       '--homedir', homedir,
                                       '--keyring', keyring,
                                       '--no-default-keyring',
                                       '--import', key])

        # Set normal permissions for the keyring since gpg creates it
        # 0600
        os.chmod(keyring, 0o0644)


def disk_usage(path):
    """Recursively gather disk usage in bytes for path"""
    total = os.stat(path, follow_symlinks=False).st_size
    for root, dirs, files in os.walk(path):
        total += sum([os.stat(os.path.join(root, name),
                              follow_symlinks=False).st_size
                      for name in dirs + files])
    return total


def retry(func, *args, max_retries=3, **kwargs):
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
            time.sleep(1)


def latest_manifest_data():
    """Read the downloaded manifest.json from the latest build"""
    path = os.path.join(os.environ['EIB_TMPDIR'], 'latest',
                        'manifest.json')
    if not os.path.exists(path):
        raise ImageBuildError('Could not find latest manifest.json at',
                              path)
    with open(path) as f:
        return json.load(f)


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
            subprocess.check_call(('udevadm', 'settle'))

        logger.info('Deleting loop %s', loop_dev)
        retry(subprocess.check_call, ('losetup', '-d', loop_dev))


def unmount_root_filesystems(root):
    """Unmount all filesystems in root path

    Finds all filesystems mounted within root (but not root itself) and
    unmounts them.
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
            # Search for mounts that begin with $dir/. The trailing / is
            # added for 2 reasons.
            #
            # 1. It makes sure that $dir itself is not matched. If
            # someone has mounted the build directory itself, that was
            # probably done intentionally and wasn't done by the
            # builder.
            #
            # 2. It ensures that only paths below $dir are matched and
            # not $dir-backup or anything else that begins with the same
            # characters.
            if mountdir.startswith(root + '/'):
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

    # Finally, delete any loops backed by the root itself
    delete_root_loops(root)


def cleanup_root(root):
    """Cleanup root for deletion

    Cleans up processes, mounts and loops for the given root path. After
    this the root should be able to be deleted.
    """
    logger.info('Killing processes in %s', root)
    kill_root_processes(root)

    logger.info('Unmounting filesystems in %s', root)
    unmount_root_filesystems(root)
