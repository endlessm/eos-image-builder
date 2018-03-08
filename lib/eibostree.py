# -*- mode: Python; coding: utf-8 -*-

# Endless image builder library - OSTree utilities
#
# Copyright (C) 2018  Endless Mobile, Inc.
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
import logging
from gi import require_version
require_version('OSTree', '1.0')
from gi.repository import GLib, OSTree

logger = logging.getLogger(__name__)


def fetch_remote_collection_id(repo, remote):
    """Fetch the remote's collection ID from its summary file

    Fetch the remote's summary and look for the
    ostree.summary.collection-id value in the summary metadata. Return
    None if no value was found.

    Args:
        repo: An open OSTree.Repo
        remote: The OSTree remote name

    Returns:
        The collection ID string or None if one isn't set
    """
    logger.info('Fetching OSTree summary for remote %s', remote)
    _, summary_bytes, _ = eib.retry(repo.remote_fetch_summary, remote)
    summary_variant_type = GLib.VariantType.new(
        OSTree.SUMMARY_GVARIANT_STRING)
    summary = GLib.Variant.new_from_bytes(summary_variant_type,
                                          summary_bytes, False)

    # Look for collection ID key in the metadata. This is
    # OSTREE_SUMMARY_COLLECTION_ID in ostree, but that's not exported.
    summary_metadata = summary[1]
    collection_id = summary_metadata.get('ostree.summary.collection-id')
    if collection_id is None:
        logger.info('No collection ID in %s summary', remote)
    else:
        logger.info('Found collection ID "%s" in %s summary',
                    collection_id, remote)
    return collection_id
