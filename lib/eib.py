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
import os
import shutil

BUILDDIR = '/var/cache/eos-image-builder'
SYSCONFDIR = '/etc/eos-image-builder'
LOCKFILE = '/var/lock/eos-image-builder.lock'

class ImageBuildError(Exception):
    """Errors from the image builder"""
    def __init__(self, *args):
        self.msg = ' '.join(map(str, args))

    def __str__(self):
        return str(self.msg)

def recreate_dir(path):
    """Delete and recreate a directory"""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)

def add_cli_options(argparser):
    """Add command line options for eos-image-builder. This allows the
    settings to be shared between eos-image-builder and run-build.
    """
    assert(isinstance(argparser, ArgumentParser))
    argparser.add_argument('-p', '--product', default='eos',
                           help='product to build')
    argparser.add_argument('-a', '--arch', help='architecture to build')
    argparser.add_argument('--platform', help='platform to build')
    argparser.add_argument('-P', '--personalities', default='base',
                           help='personalities to build')
    argparser.add_argument('-f', '--force', action='store_true',
                           help='run build even when no new assets found')
    argparser.add_argument('-n', '--dry-run', action='store_true',
                           help="don't publish images")
    argparser.add_argument('--no-checkout', action='store_true',
                           help='use current builder branch')
    argparser.add_argument('branch', nargs='?', default='master',
                           help='branch to build')
