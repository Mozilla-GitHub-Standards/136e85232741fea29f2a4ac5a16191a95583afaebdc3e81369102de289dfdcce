#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from base64 import b64decode
import contextlib
import logging
import logging.config
import os
import random
import re
import socket
import time

import boto3
import pika


PIKA_EXCEPTIONS = (
    pika.exceptions.AMQPConnectionError,
    pika.exceptions.ChannelClosed,
    pika.exceptions.ConnectionClosed,
    pika.exceptions.NoFreeChannels,
    socket.timeout
)

# NOTE(willkg): These values match Antenna throttling return values
ACCEPT = '0'
DEFER = '1'

NOVALUE = object()


logging.config.dictConfig({
    'version': 1,
    # NOTE(willkg): We don't disable existing loggers because that prevents
    # scripts that use this module from disabling logging.
    'formatters': {
        'mozlog': {
            '()': 'dockerflow.logging.JsonLogFormatter',
            'logger_name': 'antenna',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'mozlog',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'pigeon': {
            'propagate': False,
            'handlers': ['console'],
            'level': 'DEBUG',
        },
    },
})


logger = logging.getLogger('pigeon')
logger.setLevel(logging.DEBUG)


def parse_queues(val):
    """Takes a string and converts it to a list of (queuename, throttle) tuples

    :arg str val: the configuration value

    :returns: list of (throttle, queuename) tuples

    """
    # Split the value on , and drop any pre/post whitespace
    val = [part.strip() for part in val.split(',')]

    # For each thing, if there's a :, then the first part is the throttle as an
    # int and the second part is the queue name
    queues = []
    for mem in val:
        if ':' in mem:
            mem = [part.strip() for part in mem.split(':')]
            queues.append((int(mem[0]), mem[1]))
        else:
            queues.append((100, mem))

    return queues


class Config(object):
    def __init__(self):
        self.host = self.get_from_env('HOST')
        self.port = int(self.get_from_env('PORT'))
        self.user = self.get_from_env('USER')
        self.queues = parse_queues(self.get_from_env('QUEUE'))

        self.aws_region = self.get_from_env('AWS_REGION', '')

        self.env = self.get_from_env('ENV', '')

        # Get secrets last because decrypt needs previous vars to get them
        self.password = self.decrypt(self.get_from_env('PASSWORD'))
        self.virtual_host = self.decrypt(self.get_from_env('VIRTUAL_HOST'))

    def get_from_env(self, key, default=NOVALUE):
        if default is NOVALUE:
            return os.environ['PIGEON_%s' % key]
        else:
            return os.environ.get('PIGEON_%s' % key, default)

    def decrypt(self, data):
        """Decrypts config value"""
        # NOTE(willkg): Either PIGEON_AWS_REGION is set in the environment, or
        # we're running the test suite. In the latter case, we don't want to be
        # using the kms decryption and this should be a no-op.
        if not self.aws_region:
            logger.warning('Please set PIGEON_AWS_REGION. Returning original unencrypted data.')
            return data

        kwargs = {
            'region_name': self.aws_region
        }

        client = boto3.client('kms', **kwargs)
        text_as_bytes = client.decrypt(CiphertextBlob=b64decode(data))['Plaintext']
        return text_as_bytes.decode('ascii')

    @contextlib.contextmanager
    def override(self, **kwargs):
        """Context-manager in tests to override config variables

        Pass variable (lowercase) = value as args.

        """
        old_values = {}

        for key, val in kwargs.items():
            if getattr(self, key, None) is not None:
                old_values[key] = getattr(self, key)
                setattr(self, key, val)

        yield

        for key, val in old_values.items():
            setattr(self, key, val)


CONFIG = Config()


def statsd_incr(key, value=1, tags=None):
    """Sends a specially formatted line for datadog to pick up for statsd incr"""
    if CONFIG.env:
        tags = '#env:%s' % CONFIG.env
    else:
        tags = ''

    print('MONITORING|%(timestamp)s|%(val)s|count|%(key)s|%(tags)s' % {
        'timestamp': int(time.time()),
        'key': key,
        'val': value,
        'tags': tags,
    })


CRASH_ID_RE = re.compile(r"""
    ^
    [a-f0-9]{8}-
    [a-f0-9]{4}-
    [a-f0-9]{4}-
    [a-f0-9]{4}-
    [a-f0-9]{6}
    [0-9]{6}      # date in YYMMDD
    $
""", re.VERBOSE)


def is_crash_id(crash_id):
    """Verifies a given string is a crash id

    :arg str crash_id: the string in question

    :returns: True if it's a crash id and False if not

    """
    return (
        # Match structure
        bool(CRASH_ID_RE.match(crash_id)) and

        # The 7-to-last character is a throttle result
        crash_id[-7] in (ACCEPT, DEFER)
    )


def extract_crash_id_from_record(record):
    """Given a record, extracts the crash id

    :arg dict record: the AWS event record

    :returns: None (not a crash id) or the crash_id

    """
    key = 'not extracted yet'
    try:
        key = record['s3']['object']['key']
        logger.info('looking at key: %s', key)
        if not key.startswith('v2/raw_crash/'):
            logger.debug('%s: not a raw crash--ignoring', repr(key))
            return None
        crash_id = key.rsplit('/', 1)[-1]
        if not is_crash_id(crash_id):
            logger.debug('%s: not a crash id--ignoring', repr(key))
            return None
        return crash_id
    except (KeyError, IndexError) as exc:
        logger.debug(
            '%s: exception thrown when extracting crashid--ignoring: %s', repr(key), exc
        )
        return None


def get_throttle_result(crash_id):
    return crash_id[-7]


def build_pika_connection(host, port, virtual_host, user, password):
    """Build a pika (rabbitmq) connection"""
    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=host,
            port=port,
            virtual_host=virtual_host,
            connection_attempts=10,
            socket_timeout=10,
            retry_delay=1,
            credentials=pika.credentials.PlainCredentials(
                user,
                password
            )
        )
    )


def handler(event, context):
    connection = None

    accepted_records = []

    logger.info('number of records: %d', len(event['Records']))
    for record in event['Records']:
        # Skip anything that's not an S3 ObjectCreated:put event.
        if record['eventSource'] != 'aws:s3' or record['eventName'] != 'ObjectCreated:Put':
            continue

        # Extract bucket name for debugging.
        bucket = record['s3']['bucket']['name']

        # Extract crash id--if it's not a raw_crash object, skip it.
        crash_id = extract_crash_id_from_record(record)
        if crash_id is None:
            continue

        logger.info('crash id: %s in %s', crash_id, bucket)

        # Skip crashes marked DEFER
        if get_throttle_result(crash_id) == DEFER:
            statsd_incr('socorro.pigeon.defer', value=1)
            continue

        accepted_records.append(crash_id)

    if not accepted_records:
        return

    try:
        connection = build_pika_connection(
            host=CONFIG.host,
            port=CONFIG.port,
            virtual_host=CONFIG.virtual_host,
            user=CONFIG.user,
            password=CONFIG.password,
        )
        props = pika.BasicProperties(delivery_mode=2)

        channel = connection.channel()

        for crash_id in accepted_records:
            statsd_incr('socorro.pigeon.accept', value=1)

            for throttle, queue in CONFIG.queues:
                if throttle != 100 and throttle <= random.randint(0, 100):
                    logger.info('%s: crash throttled (%s:%s)', crash_id, throttle, queue)
                    statsd_incr('socorro.pigeon.throttled', value=1)
                    continue

                logger.info('%s: publishing to %s', crash_id, queue)
                channel.basic_publish(
                    exchange='',
                    routing_key=queue,
                    body=crash_id,
                    properties=props
                )

    except PIKA_EXCEPTIONS:
        # We've told the pika connection to retry a bunch, so if we hit this,
        # then evil is a foot and there isn't much we can do about it.
        statsd_incr('socorro.pigeon.pika_error', value=1)
        logger.exception('Error: amqp publish failed: %s', crash_id)
        raise

    except Exception:
        statsd_incr('socorro.pigeon.unknown_error', value=1)
        logger.exception('Error: amqp publish failed for unknown reason: %s', crash_id)
        raise

    finally:
        if connection is not None:
            connection.close()
