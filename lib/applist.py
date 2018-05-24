# -*- mode: Python; coding: utf-8 -*-

# Endless image builder: image sizing helper
#
# Copyright © 2017–2018 Endless Mobile, Inc.
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
import eibflatpak
import logging
import os

from gi import require_version
require_version('Flatpak', '1.0')
from gi.repository import Gio, GLib, Flatpak  # noqa

log = logging.getLogger(__name__)


MUST_KEEP_APPS = {
    # https://phabricator.endlessm.com/T18626#436847
    'net.gcompris.Gcompris',
    'net.sourceforge.Supertuxkart',

    # “encyclopedia is the most important app” – Nick/Matt
    'com.endlessm.encyclopedia.id',
}
PREFER_REMOVE_NS = {
    'org.kde.',
}
PREFER_REMOVE_APPS = {
    'net.sourceforge.Warmux',
    'io.github.Supertux',
    'org.marsshooter.Marsshooter',
    'net.wz2100.Warzone2100',
    'org.tuxfamily.Xmoto',
}


class AppListFormatter(object):
    def __init__(self, refs, personality, verdicts):
        locales = {
            personality,
            personality.split('_')[0],  # pt_BR -> pt
        }
        self.locale_apps = []
        self.generic_apps = []
        self.runtimes = []
        self.verdicts = verdicts

        # Rather than using a construction like '{:>{}}'.format('abc', 8), make
        # a static format string we can re-use with different data. The heading
        # and table formatting is valid Phabricator markup
        header_row = ['Remote', 'App ID', 'Installed Size', 'Download Size']
        format_strings = ['{{:{}}}', '{{:{}}}', '{{:>{}}}', '{{:>{}}}']
        if self.verdicts:
            header_row.append('Remove?')
            format_strings.append('{{:{}}}')

        widths = [len(h) for h in header_row]
        # Calculate width for first two columns; the others are no wider than
        # their header. While we're at it, partition the apps.
        remote_width, id_width = widths[:2]
        for ref in refs:
            remote_width = max(remote_width, len(ref.remote.name))
            id_width = max(id_width, len(self._describe_ref(ref)))

            if ref.kind == Flatpak.RefKind.RUNTIME:
                self.runtimes.append(ref)
            elif ref.name.split('.')[-1] in locales:
                self.locale_apps.append(ref)
            else:
                self.generic_apps.append(ref)

        widths[:2] = [remote_width, id_width]

        format_string_format = '| ' + ' | '.join(format_strings) + ' |\n'
        self.format_string = format_string_format.format(*widths)
        self.header = ''.join((
            self.format_string.format(*header_row),
            self.format_string.format(*('-' * width for width in widths)),
        ))

    @staticmethod
    def _describe_ref(ref):
        '''Returns a "unique-enough" identifier for 'ref'.

        We do not include multiple branches of the same app in our images,
        but we routinely include multiple branches of the same runtime, so need
        to include the branch to distinguish them.
        '''
        if ref.kind == Flatpak.RefKind.RUNTIME:
            # TODO: .Locale extensions for apps are also runtimes, but we don't
            # need to show the branch for them. If we had a reverse-lookup to
            # find r such that ref is in r.related_refs, we could check whether
            # the parent ref (if it exists) is also a runtime, and only add the
            # suffix in that case.
            return '//'.join((ref.name, ref.branch))
        else:
            return ref.name

    def _write_table(self, stream, title, refs, display_branch=False):
        stream.write('== {} ==\n\n'.format(title))
        stream.write(self.header)

        for ref in refs:
            row = [
                ref.remote.name,
                self._describe_ref(ref),
                GLib.format_size(ref.installed_size),
                GLib.format_size(ref.download_size),
            ]
            if self.verdicts:
                row.append(self.verdicts.get(ref.name, ''))
            stream.write(self.format_string.format(*row))

        stream.write('\n')

    def write(self, stream):
        self._write_table(stream, 'Locale-specific apps', self.locale_apps)
        self._write_table(stream, 'Generic apps', self.generic_apps)
        self._write_table(stream, 'Runtimes', self.runtimes,
                          display_branch=True)


def show_apps(config, excess, stream):
    '''Lists apps that will be installed for this image, with their installed
    (uncompressed) and download (compressed) sizes.

    If excess > 0, we also suggest which apps to remove to save that amount of
    space in the compressed image.  We use the compressed download size
    according to Flatpak as an approximation for how the uncompressed app will
    affect the compressed size in the image.

    Args:
        config (ConfigParser): the image config
        excess (int): bytes that need to be saved to fit within the size, or 0
                      if there's no size limit and you just want the list of
                      apps
        stream (file): stream to which to write the lists
    '''
    # Use a temporary repo in the image builder tmpdir where it will be
    # cleaned up later
    tmpdir = config['build']['tmpdir']
    installation_path = os.path.join(tmpdir, 'applist')
    os.makedirs(installation_path, exist_ok=True)
    installation_file = Gio.File.new_for_path(installation_path)
    installation = Flatpak.Installation.new_for_path(
        installation_file, user=False)

    # Enumerate remotes and resolve all refs needed for installation
    manager = eibflatpak.FlatpakManager(installation, config)
    manager.add_remotes()
    manager.enumerate_remotes()
    manager.resolve_refs()

    # Make a simple list of FlatpakFullRefs sorted by descending
    # download size order, then by app name
    refs = sorted(
        [install_ref.full_ref for install_ref in
         manager.install_refs.values()],
        key=lambda ref: (- ref.download_size, ref.name)
    )
    total_installed = sum(ref.installed_size for ref in refs)
    total = sum(ref.download_size for ref in refs)

    excess_after_removals = excess
    verdicts = {}

    if excess > 0:
        for name in MUST_KEEP_APPS:
            verdicts[name] = 'No'

        # Remove any app which we're happy to sacrifice
        for ref in refs:
            if excess_after_removals <= 0:
                break

            if ref.name in verdicts:
                continue

            if (
                ref.name in PREFER_REMOVE_APPS or
                any(ref.name.startswith(prefix) for prefix in PREFER_REMOVE_NS)
            ):
                verdicts[ref.name] = 'Yes'
                excess_after_removals -= ref.download_size

        # Propose removing the largest n apps until we're under budget.
        # TODO: prefer to remove generic apps?
        # TODO: some non-greedy algorithm that prefers to remove smaller apps
        # to minimize free excess_after_removals
        for ref in refs:
            if excess_after_removals <= 0:
                break

            if ref.name in verdicts:
                continue

            verdicts[ref.name] = 'Maybe'
            excess_after_removals -= ref.download_size

    stream.write('\n')
    stream.write('Estimated installed size of apps: {}\n'.format(
        GLib.format_size(total_installed)))
    stream.write('Estimated compressed size of apps: {}\n'.format(
        GLib.format_size(total)))
    if excess > 0:
        stream.write('Over budget: {}\n'.format(GLib.format_size(excess)))
        if excess_after_removals > 0:
            stream.write('Over budget after proposed removals: {}\n'.format(
                GLib.format_size(excess_after_removals)))
        else:
            stream.write('Free space after proposed removals: {}\n'.format(
                GLib.format_size(-excess_after_removals)))
    stream.write('\n')

    personality = config['build']['personality']
    AppListFormatter(refs, personality, verdicts).write(stream)
