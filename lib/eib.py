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

from argparse import ArgumentParser
import configparser
from collections import Counter
import fnmatch
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

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
        """
        sect = self[section]
        add_opts = fnmatch.filter(sect.keys(), prefix + '_add_*')
        del_opts = fnmatch.filter(sect.keys(), prefix + '_del_*')

        # If the prefix doesn't exist, merge together the add and del
        # options and set it.
        if prefix not in sect:
            add_vals = Counter()
            for opt in add_opts:
                add_vals.update(sect[opt].split())
            del_vals = Counter()
            for opt in del_opts:
                del_vals.update(sect[opt].split())

            # Set the prefix to the difference of the counters. Merge
            # the values together with newlines like they were in the
            # original configuration.
            vals = add_vals - del_vals
            sect[prefix] = '\n'.join(sorted(vals.keys()))

        # Remove the add/del options to cleanup the section
        for opt in add_opts + del_opts:
            del sect[opt]


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
    argparser.add_argument('--show-config', action='store_true',
                           help='show configuration and exit')
    argparser.add_argument('-f', '--force', action='store_true',
                           help='run build even when no new assets found')
    argparser.add_argument('-n', '--dry-run', action='store_true',
                           help="don't publish images")
    argparser.add_argument('--use-production', action='store_true',
                           help="use production ostree/flatpak repos rather than staging")
    argparser.add_argument('--checkout', action='store_true',
                           help='copy the git repo to the build directory')
    add_argument('--lock-timeout', type=int, default=LOCKTIMEOUT,
                 help='time in seconds to acquire lock before '
                      'exiting')
    add_argument('branch', nargs='?', default='master',
                 help='branch to build')


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
    retry = 0
    while True:
        try:
            return func(*args, **kwargs)
        except:
            retry += 1
            if retry > max_retries:
                print('Failed', max_retries, 'retries; giving up',
                      file=sys.stderr)
                raise
            print('Retrying attempt', retry, file=sys.stderr)
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
