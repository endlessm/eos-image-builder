# -*- mode: Python; coding: utf-8 -*-

# Endless image builder library - flatpak utilities
#
# Copyright (C) 2017  Endless Mobile, Inc.
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

import eib
from gi import require_version
require_version('Flatpak', '1.0')
from gi.repository import Flatpak, GLib


def find_runtime_for_refspec(installation, remote, refspec):
    # Fetch the metadata for the app
    ref = Flatpak.Ref.parse(refspec)
    buf = eib.retry(installation.fetch_remote_metadata_sync, remote, ref)

    # Load it into a keyfile and get the runtime
    metadata = GLib.KeyFile.new()
    metadata.load_from_bytes(buf, GLib.KeyFileFlags.NONE)
    return metadata.get_string('Application', 'runtime')


def add_runtime_and_locale(bag, runtime, arch, branch):
    bag.add('runtime/{}/{}/{}'.format(runtime, arch, branch))
    bag.add('runtime/{}.Locale/{}/{}'.format(runtime, arch, branch))


def _get_ekn_services_ver(runtime_id, branch):
    if runtime_id == 'com.endlessm.Platform' and branch in ('eos3.0', 'eos3.1'):
        return '1'
    if runtime_id == 'com.endlessm.apps.Platform':
        if branch == '1':
            return '1'
        return '2'
    return None


def get_ekn_services_to_install(requested, found_runtimes):
    if requested == 'auto':
        ekn_services_to_install = set()
        for runtime in found_runtimes:
            runtime_id, arch, branch = runtime.split('/')
            ekn_services_ver = _get_ekn_services_ver(runtime_id, branch)
            if ekn_services_ver is not None:
                ekn_services_to_install.add(ekn_services_ver)
        return ekn_services_to_install

    # Explicitly requested versions
    return set(requested.split(','))


def ekn_services_app_id(version):
    if version == '1':
        return 'com.endlessm.EknServices'
    return 'com.endlessm.EknServices{}'.format(version)


def ekn_services_refspec(version, arch):
    app_id = ekn_services_app_id(version)
    if version == '1':
        return 'app/{}/{}/eos3'.format(app_id, arch)
    return 'app/{}/{}/stable'.format(app_id, arch)
