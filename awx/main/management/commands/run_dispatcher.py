# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.
import logging
import time
import threading

from django.conf import settings
from django.core.management.base import BaseCommand
from kombu import Connection, Exchange, Queue

from awx.main.dispatch import get_local_queuename, reaper
from awx.main.dispatch.worker import AWXConsumer, TaskWorker

logger = logging.getLogger('awx.main.dispatch')


def construct_bcast_queue_name(common_name):
    return common_name.encode('utf8') + '_' + settings.CLUSTER_HOST_ID


class Command(BaseCommand):
    help = 'Launch the task dispatcher'

    def beat(self):
        '''
        <Ralph Wiggum voice> I'm a periodic scheduler!
        Seriously, though, this needs to be replaced with something to give us
        what celerybeat used to provide.  For _now_, this is good enough to
        make the TaskManager run every X seconds.

        A good candidate might be: https://apscheduler.readthedocs.io/en/latest/
        '''
        start = time.time()
        while True:
            time.sleep(1)
            elapsed = int(time.time() - start)
            for _, v in settings.CELERYBEAT_SCHEDULE.items():
                if elapsed % v['schedule'].total_seconds() == 0:
                    task = TaskWorker.resolve_callable(v['task'])
                    task.apply_async()

    def handle(self, *arg, **options):
        reaper.reap()
        beat = threading.Thread(target=self.beat)
        beat.daemon = True
        beat.start()
        consumer = None
        with Connection(settings.BROKER_URL) as conn:
            try:
                bcast = 'tower_broadcast_all'
                queues = [
                    Queue(q, Exchange(q), routing_key=q)
                    for q in (settings.AWX_CELERY_QUEUES_STATIC + [get_local_queuename()])
                ]
                queues.append(
                    Queue(
                        construct_bcast_queue_name(bcast),
                        exchange=Exchange(bcast, type='fanout'),
                        routing_key=bcast,
                        reply=True
                    )
                )
                consumer = AWXConsumer(conn, TaskWorker(), queues)
                consumer.run()
            except KeyboardInterrupt:
                logger.debug('Terminating Task Dispatcher')
                if consumer:
                    consumer.stop()
