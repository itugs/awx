import logging
import os
import traceback

from multiprocessing import Process
from multiprocessing import Queue as MPQueue
from Queue import Full as QueueFull

from django.conf import settings
from django.db import connection as django_connection
from django.core.cache import cache as django_cache

logger = logging.getLogger('awx.main.dispatch')


class WorkerPool(object):
    '''
    Creates a pool of forked worker processes, each of which has an associated
    multiprocessing.Queue.

    As WorkerPool.write(...) is called (generally, by a kombu consumer
    implementation when it receives an AMQP message), messages are passed to
    one of the multiprocessing Queues where some work can be done on them.

    class MessagePrinter(awx.main.dispatch.worker.BaseWorker):

        def perform_work(self, body):
            print body

    pool = WorkerPool(min_workers=4)  # spawn four worker processes
    pool.init_workers(MessagePrint().work_loop)
    pool.write(
        0,  # preferred worker 0
        'Hello, World!'
    )
    '''

    def __init__(self, min_workers=None, queue_size=None):
        self.min_workers = min_workers or settings.JOB_EVENT_WORKERS
        self.queue_size = queue_size or settings.JOB_EVENT_MAX_QUEUE_SIZE

        # self.workers tracks the state of worker running worker processes:
        # [
        #   (total_messages_consumed, multiprocessing.Queue, multiprocessing.Process),
        #   (total_messages_consumed, multiprocessing.Queue, multiprocessing.Process),
        #   (total_messages_consumed, multiprocessing.Queue, multiprocessing.Process),
        #   (total_messages_consumed, multiprocessing.Queue, multiprocessing.Process)
        # ]
        self.workers = []

    def __len__(self):
        return len(self.workers)

    def init_workers(self, target, *target_args):
        # It's important to close these because we're _about_ to fork, and we
        # don't want the forked processes to inherit the open sockets
        # for the DB and memcached connections (that way lies race conditions)
        django_connection.close()
        django_cache.close()
        for idx in range(self.min_workers):
            queue_actual = MPQueue(self.queue_size)
            w = Process(target=target, args=(queue_actual, idx,) + target_args)
            w.start()
            self.workers.append([0, queue_actual, w])

    def write(self, preferred_queue, body):
        queue_order = sorted(range(self.min_workers), cmp=lambda x, y: -1 if x==preferred_queue else 0)
        write_attempt_order = []
        for queue_actual in queue_order:
            try:
                worker_actual = self.workers[queue_actual]
                worker_actual[1].put(body, block=True, timeout=5)
                uuid = 'message'
                if isinstance(body, dict) and 'uuid' in body:
                    uuid = body['uuid']
                logger.debug('delivered {} to worker[{}] qsize {}'.format(
                    uuid,
                    queue_actual, worker_actual[1].qsize()
                ))
                worker_actual[0] += 1
                return queue_actual
            except QueueFull:
                pass
            except Exception:
                tb = traceback.format_exc()
                logger.warn("could not write to queue %s" % preferred_queue)
                logger.warn("detail: {}".format(tb))
            write_attempt_order.append(preferred_queue)
        logger.warn("could not write payload to any queue, attempted order: {}".format(write_attempt_order))
        return None

    def stop(self, signum):
        try:
            for worker in self.workers:
                _, _, process = worker
                logger.debug('terminating worker pid:{}'.format(process.pid))
                os.kill(process.pid, signum)
        except Exception:
            logger.exception('error in shutdown_handler')
