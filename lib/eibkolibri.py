# Endless image builder library - Kolibri utilities
#
# Copyright © 2023 Endless OS Foundation LLC
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
from netrc import netrc
import os
import requests
from time import sleep
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


def get_job_status(session, base_url, job_id):
    """Get remote Kolibri job status"""
    url = urljoin(base_url, f'api/tasks/tasks/{job_id}/')
    with session.get(url) as resp:
        resp.raise_for_status()
        return resp.json()


def wait_for_job(session, base_url, job_id):
    """Wait for remote Kolibri job to complete"""
    logger.debug(f'Waiting for job {job_id} to complete')
    last_marker = None
    while True:
        data = get_job_status(session, base_url, job_id)

        # See the kolibri.core.tasks.job.State class for potential states
        # https://github.com/learningequality/kolibri/blob/develop/kolibri/core/tasks/job.py#L17
        status = data['status']
        if status == 'FAILED':
            logger.error(
              f'Job {job_id} failed: {data["exception"]}\n{data["traceback"]}'
            )
            raise Exception(f'Job {job_id} failed')
        elif status == 'CANCELED':
            raise Exception(f'Job {job_id} cancelled')
        elif status == 'COMPLETED':
            if last_marker < 100:
                logger.info('Progress: 100%')
            break

        pct = int(data['percentage'] * 100)
        marker = pct - pct % 5
        if last_marker is None or marker > last_marker:
            logger.info(f'Progress: {pct}%')
            last_marker = marker

        # Wait a bit before checking the status again.
        sleep(0.5)


def channel_exists(session, base_url, channel_id):
    """Check if channel exists on remote Kolibri server"""
    url = urljoin(base_url, f'api/content/channel/{channel_id}/')
    logger.debug(f'Checking if channel {channel_id} exists')
    with session.get(url) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            if resp.status_code == 404:
                return False
            logger.error('Failed to check channel existence: %s', resp.json())
            raise
        else:
            return True


def import_channel(session, base_url, channel_id):
    """Import channel on remote Kolibri server"""
    url = urljoin(base_url, 'api/tasks/tasks/startremotechannelimport/')
    data = {'channel_id': channel_id}
    logger.info(f'Importing channel {channel_id} metadata')
    with session.post(url, json=data) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.error('Failed to import channel: %s', resp.json())
            raise
        job = resp.json()
    wait_for_job(session, base_url, job['id'])


def import_content(session, base_url, channel_id):
    """Import channel content on remote Kolibri server"""
    url = urljoin(base_url, 'api/tasks/tasks/startremotecontentimport/')
    data = {
        'channel_id': channel_id,
        # Fetch all nodes so that the channel is fully mirrored.
        'renderable_only': False,
        'fail_on_error': True,
    }
    logger.info(f'Importing channel {channel_id} content')
    with session.post(url, json=data) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.error('Failed to import content: %s', resp.json())
            raise
        job = resp.json()
    wait_for_job(session, base_url, job['id'])


def diff_channel(session, base_url, channel_id):
    """Generate channel diff on remote Kolibri server"""
    url = urljoin(base_url, 'api/tasks/tasks/channeldiffstats/')
    data = {'channel_id': channel_id, 'method': 'network'}
    logger.info(f'Generating channel {channel_id} diff')
    with session.post(url, json=data) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.error('Failed to generate channel diff: %s', resp.json())
            raise
        job = resp.json()
    wait_for_job(session, base_url, job['id'])


def update_channel(session, base_url, channel_id):
    """Update channel on remote Kolibri server"""
    url = urljoin(base_url, 'api/tasks/tasks/startchannelupdate/')
    data = {
        'channel_id': channel_id,
        'sourcetype': 'remote',
        # Fetch all nodes so that the channel is fully mirrored.
        'renderable_only': False,
        'fail_on_error': True,
    }
    logger.info(f'Updating channel {channel_id} content')
    with session.post(url, json=data) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.error('Failed to update channel: %s', resp.json())
            raise
        job = resp.json()
    wait_for_job(session, base_url, job['id'])


def seed_remote_channels(channel_ids):
    """Import channels and content on remote Kolibri server"""
    config = eib.get_config()

    base_url = config.get('kolibri', 'central_content_base_url', fallback=None)
    if not base_url:
        logger.info('Not using custom Kolibri content server')
        return

    netrc_path = os.path.join(eib.SYSCONFDIR, 'netrc')
    if not os.path.exists(netrc_path):
        logger.info(f'No credentials in {netrc_path}')
        return

    netrc_creds = netrc(netrc_path)
    host = urlparse(base_url).netloc
    creds = netrc_creds.authenticators(host)
    if not creds:
        logger.info(f'No credentials for {host} in {netrc_path}')
        return
    username, _, password = creds

    # Start a requests session with the credentials.
    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({
        'Content-Type': 'application/json',
    })

    for channel in channel_ids:
        logger.info(f'Seeding channel {channel} on {host}')

        # If the channel exists, update it since Kolibri won't import
        # new content nodes otherwise.
        if channel_exists(session, base_url, channel):
            diff_channel(session, base_url, channel)
            update_channel(session, base_url, channel)
        else:
            import_channel(session, base_url, channel)
            import_content(session, base_url, channel)
