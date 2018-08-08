import inspect
import logging
import sys
from uuid import uuid4

from django.conf import settings
from kombu import Connection, Exchange, Producer

logger = logging.getLogger('awx.main.dispatch')


def serialize_task(f):
    return '.'.join([f.__module__, f.__name__])


def task(queue=None, exchange_type=None):
    """
    Used to decorate a function or class so that it can be run asynchronously
    via the task dispatcher.  Tasks can be simple functions:

    @task()
    def add(a, b):
        return a + b

    ...or classes that define a `run` method:

    @task()
    class Adder:
        def run(self, a, b):
            return a + b

    # Tasks can be run synchronously...
    assert add(1, 1) == 2
    assert Adder().run(1, 1) == 2

    # ...or published to a queue:
    add.apply_async([1, 1])
    Adder.apply_async([1, 1])

    # Tasks can also define a specific target queue or exchange type:

    @task(queue='slow-tasks')
    def snooze():
        time.sleep(10)

    @task(queue='tower_broadcast', exchange_type='fanout')
    def announce():
        print "Run this everywhere!"
    """
    class wrapped:
        def __init__(self, f):
            setattr(f, 'apply_async', self.apply_async)
            setattr(f, 'delay', self.delay)
            self.wrapped = f
            self.queue = queue

        def delay(self, *args, **kwargs):
            return self.apply_async(args, kwargs)

        @property
        def name(self):
            return serialize_task(self.wrapped)

        def apply_async(self, args=None, kwargs=None, queue=None, uuid=None, **kw):
            task_id = uuid or str(uuid4())
            args = args or []
            kwargs = kwargs or {}
            queue = queue or self.queue or settings.CELERY_DEFAULT_QUEUE
            obj = {
                'uuid': task_id,
                'args': args,
                'kwargs': kwargs,
                'task': self.name
            }
            obj.update(**kw)
            if callable(queue):
                queue = queue()
            if not settings.IS_TESTING(sys.argv):
                with Connection(settings.BROKER_URL) as conn:
                    exchange = Exchange(queue, type=exchange_type or 'direct')
                    producer = Producer(conn)
                    logger.debug('publish {}({}, queue={})'.format(
                        self.name,
                        task_id,
                        queue
                    ))
                    producer.publish(obj,
                                     serializer='json',
                                     compression='bzip2',
                                     exchange=exchange,
                                     declare=[exchange],
                                     delivery_mode="persistent",
                                     routing_key=queue)
            return (obj, queue)

        def __call__(self, *args, **kwargs):
            # Decorator magic ahead...
            if inspect.isclass(self.wrapped):
                # If this decorator is wrapping a _class_, then the function
                # we're wrapping here is __init__; in this situation, we want
                # to return the instantiated instance (what __init__ of the
                # wrapped class would return if you called it)
                #
                # @task()
                # class SomeTask:
                #     def run(self, a, b):
                #         return a + b
                #
                # task = SomeTask()  <--- this wrapped callable
                # task.run(1, 1)
                return self.wrapped()
            # Otherwise, we're just wrapping a function, and we want to just call
            # the function itself
            return self.wrapped(*args, **kwargs)

    return wrapped
