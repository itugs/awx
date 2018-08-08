# Copyright (c) 2018 Ansible by Red Hat
# All Rights Reserved.

import logging
import signal
from uuid import UUID
from Queue import Empty as QueueEmpty

from kombu.mixins import ConsumerMixin

from awx.main.dispatch.pool import WorkerPool

logger = logging.getLogger('awx.main.dispatch')


def signame(sig):
    return dict(
        (k, v) for v, k in signal.__dict__.items()
        if v.startswith('SIG') and not v.startswith('SIG_')
    )[sig]


class WorkerSignalHandler:

    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, *args, **kwargs):
        self.kill_now = True


class AWXConsumer(ConsumerMixin):

    def __init__(self, connection, worker, queues=[]):
        self.connection = connection
        self.total_messages = 0
        self.queues = queues
        self.worker = worker
        self.pool = WorkerPool()
        self.pool.init_workers(self.worker.work_loop)

    def get_consumers(self, Consumer, channel):
        logger.debug("Listening on {}".format(self.queues))
        return [Consumer(queues=self.queues, accept=['json'],
                         callbacks=[self.process_task])]

    def process_task(self, body, message):
        if "uuid" in body and body['uuid']:
            try:
                queue = UUID(body['uuid']).int % len(self.pool)
            except Exception:
                queue = self.total_messages % len(self.pool)
        else:
            queue = self.total_messages % len(self.pool)
        self.pool.write(queue, body)
        self.total_messages += 1
        message.ack()

    def run(self, *args, **kwargs):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.worker.on_start()
        super(AWXConsumer, self).run(*args, **kwargs)

    def stop(self, signum, frame):
        self.should_stop = True
        logger.debug('received {}, stopping'.format(signame(signum)))
        self.worker.on_stop()
        self.pool.stop(signum)


class BaseWorker(object):

    def work_loop(self, queue, idx, *args):
        signal_handler = WorkerSignalHandler()
        while not signal_handler.kill_now:
            try:
                body = queue.get(block=True, timeout=1)
            except QueueEmpty:
                continue
            except Exception as e:
                logger.error("Exception on worker, restarting: " + str(e))
                continue
            self.perform_work(body, *args)

    def perform_work(self, body):
        raise NotImplemented()

    def on_start(self):
        pass

    def on_stop(self):
        pass
