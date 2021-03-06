from __future__ import absolute_import

import socket
import threading

from kombu.common import ignore_errors

from celery.datastructures import AttributeDict
from celery.utils.log import get_logger

from . import control

logger = get_logger(__name__)
debug, error, info = logger.debug, logger.error, logger.info


class Pidbox(object):
    consumer = None

    def __init__(self, c):
        self.c = c
        self.hostname = c.hostname
        self.node = c.app.control.mailbox.Node(c.hostname,
            handlers=control.Panel.data,
            state=AttributeDict(app=c.app, hostname=c.hostname, consumer=c),
        )

    def on_message(self, body, message):
        try:
            self.node.handle_message(body, message)
        except KeyError as exc:
            error('No such control command: %s', exc)
        except Exception as exc:
            error('Control command error: %r', exc, exc_info=True)
            self.reset()

    def start(self, c):
        self.node.channel = c.connection.channel()
        self.consumer = self.node.listen(callback=self.on_message)

    def stop(self, c):
        self.consumer = self._close_channel(c)

    def reset(self):
        """Sets up the process mailbox."""
        self.stop(self.c)
        self.start(self.c)

    def _close_channel(self, c):
        if self.node and self.node.channel:
            ignore_errors(c, self.node.channel.close)

    def shutdown(self, c):
        if self.consumer:
            debug('Cancelling broadcast consumer...')
            ignore_errors(c, self.consumer.cancel)
        self.stop(self.c)


class gPidbox(Pidbox):
    _node_shutdown = None
    _node_stopped = None
    _resets = 0

    def start(self, c):
        c.pool.spawn_n(self.loop, c)

    def stop(self, c):
        if self._node_stopped:
            self._node_shutdown.set()
            debug('Waiting for broadcast thread to shutdown...')
            self._node_stopped.wait()
            self._node_stopped = self._node_shutdown = None
        super(gPidbox, self).stop(c)

    def reset(self):
        self._resets += 1

    def _do_reset(self, c, connection):
        self._close_channel(c)
        self.node.channel = connection.channel()
        self.consumer = self.node.listen(callback=self.on_message)
        self.consumer.consume()

    def loop(self, c):
        resets = [self._resets]
        shutdown = self._node_shutdown = threading.Event()
        stopped = self._node_stopped = threading.Event()
        try:
            with c.connect() as connection:

                info('pidbox: Connected to %s.', connection.as_uri())
                self._do_reset(c, connection)
                while not shutdown.is_set() and c.connection:
                    if resets[0] < self._resets:
                        resets[0] += 1
                        self._do_reset(c, connection)
                    try:
                        connection.drain_events(timeout=1.0)
                    except socket.timeout:
                        pass
        finally:
            stopped.set()
