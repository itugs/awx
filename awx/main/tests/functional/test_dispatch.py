import datetime
import multiprocessing
import random
import signal

from django.utils.timezone import now as tz_now
import pytest

from awx.main.models import Job, WorkflowJob, Instance
from awx.main.dispatch import reaper
from awx.main.dispatch.pool import WorkerPool
from awx.main.dispatch.publish import task
from awx.main.dispatch.worker import BaseWorker, TaskWorker


@task()
def add(a, b):
    return a + b


@task()
class Adder:
    def run(self, a, b):
        return add(a, b)


@task(queue='hard-math')
def multiply(a, b):
    return a * b


class SimpleWorker(BaseWorker):

    def perform_work(self, body, *args):
        pass


class ResultWriter(BaseWorker):

    def perform_work(self, body, result_queue):
        result_queue.put(body + '!!!')


@pytest.mark.django_db
class TestWorkerPool:

    def setup_method(self, test_method):
        self.pool = WorkerPool(min_workers=3)

    def teardown_method(self, test_method):
        self.pool.stop(signal.SIGTERM)

    def test_worker(self):
        self.pool.init_workers(SimpleWorker().work_loop)
        assert len(self.pool) == 3
        for worker in self.pool.workers:
            total, _, process = worker
            assert total == 0
            assert process.is_alive() is True

    def test_single_task(self):
        self.pool.init_workers(SimpleWorker().work_loop)
        self.pool.write(0, 'xyz')
        assert self.pool.workers[0][0] == 1  # worker at index 0 handled one task
        assert self.pool.workers[1][0] == 0
        assert self.pool.workers[2][0] == 0

    def test_queue_preference(self):
        self.pool.init_workers(SimpleWorker().work_loop)
        self.pool.write(2, 'xyz')
        assert self.pool.workers[0][0] == 0
        assert self.pool.workers[1][0] == 0
        assert self.pool.workers[2][0] == 1  # worker at index 2 handled one task

    def test_worker_processing(self):
        result_queue = multiprocessing.Queue()
        self.pool.init_workers(ResultWriter().work_loop, result_queue)
        for i in range(10):
            self.pool.write(
                random.choice(self.pool.workers)[0],
                'Hello, Worker {}'.format(i)
            )
        all_messages = [result_queue.get(timeout=1) for i in range(10)]
        all_messages.sort()
        assert all_messages == [
            'Hello, Worker {}!!!'.format(i)
            for i in range(10)
        ]

        total_handled = sum([worker[0] for worker in self.pool.workers])
        assert total_handled == 10


class TestTaskDispatcher:

    @property
    def tm(self):
        return TaskWorker()

    def test_function_dispatch(self):
        result = self.tm.perform_work({
            'task': 'awx.main.tests.functional.test_dispatch.add',
            'args': [2, 2]
        })
        assert result == 4

    def test_method_dispatch(self):
        result = self.tm.perform_work({
            'task': 'awx.main.tests.functional.test_dispatch.Adder',
            'args': [2, 2]
        })
        assert result == 4


class TestTaskPublisher:

    def test_function_callable(self):
        assert add(2, 2) == 4

    def test_method_callable(self):
        assert Adder().run(2, 2) == 4

    def test_function_apply_async(self):
        message, queue = add.apply_async([2, 2])
        assert message['args'] == [2, 2]
        assert message['kwargs'] == {}
        assert message['task'] == 'awx.main.tests.functional.test_dispatch.add'
        assert queue == 'awx_private_queue'

    def test_method_apply_async(self):
        message, queue = Adder.apply_async([2, 2])
        assert message['args'] == [2, 2]
        assert message['kwargs'] == {}
        assert message['task'] == 'awx.main.tests.functional.test_dispatch.Adder'
        assert queue == 'awx_private_queue'

    def test_apply_with_queue(self):
        message, queue = add.apply_async([2, 2], queue='abc123')
        assert queue == 'abc123'

    def test_queue_defined_in_task_decorator(self):
        message, queue = multiply.apply_async([2, 2])
        assert queue == 'hard-math'

    def test_queue_overridden_from_task_decorator(self):
        message, queue = multiply.apply_async([2, 2], queue='not-so-hard')
        assert queue == 'not-so-hard'

    def test_apply_with_callable_queuename(self):
        message, queue = add.apply_async([2, 2], queue=lambda: 'called')
        assert queue == 'called'


yesterday = tz_now() - datetime.timedelta(days=1)


@pytest.mark.django_db
class TestJobReaper(object):

    @pytest.mark.parametrize('status, execution_node, controller_node, modified, fail', [
        ('running', '', '', None, False),        # running, not assigned to the instance
        ('running', 'awx', '', None, True),      # running, has the instance as its execution_node
        ('running', '', 'awx', None, True),      # running, has the instance as its controller_node
        ('waiting', '', '', None, False),        # waiting, not assigned to the instance
        ('waiting', 'awx', '', None, False),     # waiting, was edited less than a minute ago
        ('waiting', '', 'awx', None, False),     # waiting, was edited less than a minute ago
        ('waiting', 'awx', '', yesterday, True), # waiting, assigned to the execution_node, stale
        ('waiting', '', 'awx', yesterday, True), # waiting, assigned to the controller_node, stale
    ])
    def test_should_reap(self, status, fail, execution_node, controller_node, modified):
        i = Instance(hostname='awx')
        i.save()
        j = Job(
            status=status,
            execution_node=execution_node,
            controller_node=controller_node,
            start_args='SENSITIVE',
        )
        j.save()
        if modified:
            # we have to edit the modification time _without_ calling save()
            # (because .save() overwrites it to _now_)
            Job.objects.filter(id=j.id).update(modified=modified)
        reaper.reap(i)
        job = Job.objects.first()
        if fail:
            assert job.status == 'failed'
            assert 'marked as failed' in job.job_explanation
            assert job.start_args == ''
        else:
            assert job.status == status

    def test_workflow_does_not_reap(self):
        i = Instance(hostname='awx')
        i.save()
        j = WorkflowJob(
            status='running',
            execution_node='awx'
        )
        j.save()
        reaper.reap(i)

        assert WorkflowJob.objects.first().status == 'running'
