import logging
import json
from pprint import pformat  # noqa
import uuid
from random import randrange

import pika
from banal import ensure_list
from followthemoney import model
from ftmstore import get_dataset
from prometheus_client import Info
from servicelayer.cache import get_redis
from servicelayer import settings as sls
from servicelayer.taskqueue import (
    Worker,
    Task,
    Dataset,
    dataset_from_collection_id,
    get_rabbitmq_connection,
)

from ingestors import __version__
from ingestors.manager import Manager
from ingestors.analysis import Analyzer
from ingestors import settings

log = logging.getLogger(__name__)


# ToDo: Move to servicelayer??
def queue_task(collection_id, stage, job_id=None, context=None, **payload):
    task_id = uuid.uuid4().hex
    priority = randrange(1, sls.RABBITMQ_MAX_PRIORITY + 1)
    body = {
        "collection_id": dataset_from_collection_id(collection_id),
        "job_id": job_id,
        "task_id": task_id,
        "operation": stage,
        "context": context,
        "payload": payload,
        "priority": priority,
    }

    try:
        connection = get_rabbitmq_connection()
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish(
            exchange="",
            routing_key=body["operation"],
            body=json.dumps(body),
            properties=pika.BasicProperties(
                delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE, priority=priority
            ),
            mandatory=True,
        )
        dataset = Dataset(
            conn=get_redis(), name=dataset_from_collection_id(collection_id)
        )
        dataset.add_task(task_id, stage)
        channel.close()
    except Exception:
        log.exception(f"Error while queuing task: {task_id}")


SYSTEM = Info("ingestfile_system", "ingest-file system information")
SYSTEM.info({"ingestfile_version": __version__})


class IngestWorker(Worker):
    """A long running task runner that uses Redis as a task queue"""

    def _ingest(self, ftmstore_dataset, task: Task):
        manager = Manager(ftmstore_dataset, task)
        entity = model.get_proxy(task.payload)
        log.debug(f"Ingest: {entity}")
        try:
            manager.ingest_entity(entity)
        finally:
            manager.close()
        return list(manager.emitted)

    def _analyze(self, ftmstore_dataset, task: Task):
        entity_ids = set(task.payload.get("entity_ids"))
        analyzer = None
        for entity in ftmstore_dataset.partials(entity_id=entity_ids):
            if analyzer is None or analyzer.entity.id != entity.id:
                if analyzer is not None:
                    entity_ids.update(analyzer.flush())
                # log.debug("Analyze: %r", entity)
                analyzer = Analyzer(ftmstore_dataset, entity, task.context)
            analyzer.feed(entity)
        if analyzer is not None:
            entity_ids.update(analyzer.flush())
        return list(entity_ids)

    def dispatch_task(self, task: Task) -> Task:
        log.info(
            f"Task [collection:{task.collection_id}]: "
            f"op:{task.operation} task_id:{task.task_id} priority: {task.priority} (started)"
        )
        name = task.context.get("ftmstore", task.collection_id)
        ftmstore_dataset = get_dataset(name, task.operation)
        if task.operation == settings.STAGE_INGEST:
            entity_ids = self._ingest(ftmstore_dataset, task)
            payload = {"entity_ids": entity_ids}
            self.dispatch_pipeline(task, payload)
        elif task.operation == settings.STAGE_ANALYZE:
            entity_ids = self._analyze(ftmstore_dataset, task)
            payload = {"entity_ids": entity_ids}
            self.dispatch_pipeline(task, payload)
        log.info(
            f"Task [collection:{task.collection_id}]: "
            f"op:{task.operation} task_id:{task.task_id} priority: {task.priority} (done)"
        )
        return task

    def dispatch_pipeline(self, task: Task, payload):
        """Some queues use a continuation passing style pipeline argument
        to specify the next steps for a processing chain."""
        pipeline = ensure_list(task.context.get("pipeline"))
        if len(pipeline) == 0:
            return
        next_stage = pipeline.pop(0)
        context = dict(task.context)
        context["pipeline"] = pipeline

        queue_task(
            task.collection_id,
            next_stage,
            task.job_id,
            context,
            **payload,
        )


def get_worker(num_threads=None):
    qos_mapping = {
        settings.STAGE_INGEST: settings.RABBITMQ_QOS_INGEST_QUEUE,
        settings.STAGE_ANALYZE: settings.RABBITMQ_QOS_ANALYZE_QUEUE,
    }

    ingest_worker_queues = list(qos_mapping.keys())

    log.info(f"Worker active, stages: {ingest_worker_queues}")

    return IngestWorker(
        queues=ingest_worker_queues,
        conn=get_redis(),
        version=__version__,
        num_threads=num_threads,
        prefetch_count_mapping=qos_mapping,
    )
