# -*- test-case-name: vumi.workers.blinkenlights.tests.test_metrics -*-

import time
import random
import hashlib

from twisted.python import log
from twisted.internet.defer import inlineCallbacks, Deferred
from twisted.internet import reactor
from twisted.internet.task import LoopingCall

from vumi.service import Consumer, Publisher, Worker
from vumi.blinkenlights.metrics import (MetricsConsumer, MetricManager, Count,
                                        Metric, Timer, Aggregator)
from vumi.blinkenlights.message20110818 import MetricMessage


class AggregatedMetricConsumer(Consumer):
    """Consumer for aggregate metrics.

    Parameters
    ----------
    callback : function (metric_name, values)
        Called for each metric datapoint as it arrives.  The
        parameters are metric_name (str) and values (a list of
        timestamp and value pairs).
    """
    exchange_name = "vumi.metrics.aggregates"
    exchange_type = "direct"
    durable = True
    routing_key = "vumi.metrics.aggregates"

    def __init__(self, callback):
        self.queue_name = self.routing_key
        self.callback = callback

    def consume_message(self, vumi_message):
        msg = MetricMessage.from_dict(vumi_message.payload)
        for metric_name, _aggregators, values in msg.datapoints():
            self.callback(metric_name, values)


class AggregatedMetricPublisher(Publisher):
    """Publishes aggregated metrics.
    """
    exchange_name = "vumi.metrics.aggregates"
    exchange_type = "direct"
    durable = True
    routing_key = "vumi.metrics.aggregates"

    def publish_aggregate(self, metric_name, timestamp, value):
        # TODO: perhaps change interface to publish multiple metrics?
        msg = MetricMessage()
        msg.append((metric_name, "", [(timestamp, value)]))
        self.publish_message(msg)


class TimeBucketConsumer(Consumer):
    """Consume time bucketed metric messages.

    Parameters
    ----------
    bucket : int
        Bucket to consume time buckets from.
    callback : function, f(metric_name, aggregators, values)
        Called for each metric datapoint as it arrives.
        The parameters are metric_name (str),
        aggregator (list of aggregator names) and values (a
        list of timestamp and value pairs).
    """
    exchange_name = "vumi.metrics.buckets"
    exchange_type = "direct"
    durable = True
    ROUTING_KEY_TEMPLATE = "bucket.%d"

    def __init__(self, bucket, callback):
        self.queue_name = self.ROUTING_KEY_TEMPLATE % bucket
        self.callback = callback

    def consume_message(self, vumi_message):
        msg = MetricMessage.from_dict(vumi_message.payload)
        for metric_name, aggregators, values in msg.datapoints():
            self.callback(metric_name, aggregators, values)


class TimeBucketPublisher(Publisher):
    """Publish time bucketed metric messages.

    Parameters
    ----------
    buckets : int
        Total number of buckets messages are being
        distributed to.
    bucket_size : int, in seconds
        Size of each time bucket in seconds.
    """
    exchange_name = "vumi.metrics.buckets"
    exchange_type = "direct"
    durable = True
    ROUTING_KEY_TEMPLATE = "bucket.%d"

    def __init__(self, buckets, bucket_size):
        self.buckets = buckets
        self.bucket_size = bucket_size

    def find_bucket(self, metric_name, ts_key):
        md5 = hashlib.md5("%s:%d" % (metric_name, ts_key))
        return int(md5.hexdigest(), 16) / self.buckets

    def publish_metric(self, metric_name, aggregates, values):
        timestamp_buckets = {}
        for timestamp, value in values:
            ts_key = timestamp / self.bucket_size
            ts_bucket = timestamp_buckets.get(ts_key)
            if ts_bucket is None:
                ts_bucket[ts_key] = []
            ts_bucket.append((timestamp, value))

        for ts_key, ts_bucket in timestamp_buckets.iteritems():
            bucket = self.find_bucket(metric_name, ts_key)
            routing_key = self.ROUTING_KEY_TEMPLATE % bucket
            msg = MetricMessage()
            msg.extend((metric_name, aggregates, ts_bucket))
            self.publish_message(msg, routing_key=routing_key)


class MetricTimeBucket(Worker):
    """Gathers metrics messages and redistributes them to aggregators.

    :class:`MetricTimeBuckets` take metrics from the vumi.metrics
    exchange and redistribute them to one of N :class:`MetricAggregator`
    workers.

    There can be any number of :class:`MetricTimeBucket` workers.

    Configuration Values
    --------------------
    buckets : int (N)
        The total number of aggregator workers. :class:`MetricAggregator`
        workers must be started with bucket numbers 0 to N-1 otherwise
        metric data will go missing (or at best be stuck in a queue
        somewhere).
    bucket_size : int, in seconds
        The amount of time each time bucket represents.
    """
    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting a MetricTimeBucket with config: %s" % self.config)
        buckets = int(self.config.get("buckets"))
        log.msg("Total number of buckets %d" % buckets)
        bucket_size = float(self.config.get("bucket_size"))
        log.msg("Bucket size is %d seconds" % bucket_size)
        self.publisher = yield self.start_publisher(TimeBucketPublisher,
                                                    buckets, bucket_size)
        self.consumer = yield self.start_consumer(MetricsConsumer,
                self.publisher.publish_metric)


class MetricAggregator(Worker):
    """Gathers a subset of metrics and aggregates them.

    :class:`MetricAggregators` work in sets of N.

    Configuration Values
    --------------------
    bucket : int, 0 to N-1
        An aggregator needs to know which number out of N it is. This is
        its bucket number.
    bucket_size : int, in seconds
        The amount of time each time bucket represents.
    """
    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting a MetricAggregator with config: %s" % self.config)
        bucket = int(self.config.get("bucket"))
        log.msg("MetricAggregator bucket %d" % bucket)
        self.bucket_size = float(self.config.get("bucket_size"))
        log.msg("Bucket size is %d seconds" % self.bucket_size)

        # ts_key -> { metric_name -> (aggregate_set, values) }
        # values is a list of (timestamp, value) pairs
        self.buckets = {}

        self.publisher = yield self.start_publisher(AggregatedMetricPublisher)
        self.consumer = yield self.start_consumer(TimeBucketConsumer,
                                                  bucket, self.consume_metric)

        self._task = LoopingCall(self._check_buckets)
        done = self._task.start(self.bucket_size)
        done.addErrback(lambda failure: log.err(failure,
                        "MetricAggregator bucket checking task died"))

    def check_buckets(self):
        """Periodically clean out old buckets and calculate aggregates."""
        # key for previous bucket
        prev_ts_key = (int(time.time()) / self.bucket_size) - 1
        prev_ts = prev_ts_key * self.bucket_size
        for ts_key in self.buckets.keys():
            if ts_key < prev_ts_key:
                log.warn("Throwing way old metric data %r" %
                         self.buckets[ts_key])
                del self.buckets[ts_key]
            elif ts_key == prev_ts_key:
                aggregates = []
                for metric_name, (agg_set, values) in self.buckets[ts_key]:
                    for agg_name in agg_set:
                        agg_metric = "%s.%s" % (metric_name, agg_name)
                        agg_value = Aggregator.from_name(agg_name)(values)
                        aggregates.append(agg_metric, agg_value)

                for agg_metric, agg_value in aggregates:
                    self.publisher.publish_aggregate(agg_metric, prev_ts,
                                                     agg_value)
                del self.buckets[ts_key]

    def consume_metric(self, metric_name, aggregates, values):
        if not values:
            return
        ts_key = values[0] / self.bucket_size
        metrics = self.buckets.get(ts_key, None)
        if metrics is None:
            metrics = self.buckets[ts_key] = {}
        metric = metrics.get(metric_name)
        if metric is None:
            metric = metrics[metric_name] = (set(), [])
        existing_aggregates, existing_values = metric
        existing_aggregates.update(aggregates)
        existing_values.extend(values)

    def stopWorker(self):
        self._task.stop()
        self.check_buckets()


class GraphitePublisher(Publisher):
    """Publisher for sending messages to Graphite."""

    exchange_name = "graphite"
    exchange_type = "topic"
    durable = True
    auto_delete = False
    delivery_mode = 2

    def _local_timestamp(self, timestamp):
        """Graphite requires local timestamps."""
        # TODO: investigate whether graphite can be encourage to use UTC
        #       timestamps
        return timestamp - time.timezone

    def publish_metric(self, metric, value, timestamp):
        timestamp = self._local_timestamp(timestamp)
        self.publish_raw("%f %d" % (value, timestamp), routing_key=metric)


class GraphiteMetricsCollector(Worker):
    """Worker that collects Vumi metrics and publishes them to Graphite."""

    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting the GraphiteMetricsCollector with"
                " config: %s" % self.config)
        self.graphite_publisher = yield self.start_publisher(GraphitePublisher)
        self.consumer = yield self.start_consumer(AggregatedMetricConsumer,
                                                  self.consume_metrics)

    def consume_metrics(self, metric_name, values):
        for timestamp, value in values:
            self.graphite_publisher.publish_metric(metric_name, value,
                                                   timestamp)

    def stopWorker(self):
        log.msg("Stopping the GraphiteMetricsCollector")


class RandomMetricsGenerator(Worker):
    """Worker that publishes a set of random metrics.

    Useful for tests and demonstrations.

    Configuration Values
    --------------------
    manager_period : float in seconds, optional
        How often to have the internal metric manager send metrics
        messages. Default is 5s.
    generator_period: float in seconds, optional
        How often the random metric loop should send values to the
        metric manager. Default is 1s.
    """

    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting the MetricsGenerator with config: %s" % self.config)
        manager_period = float(self.config.get("manager_period", 5.0))
        log.msg("MetricManager will sent metrics every %s seconds" %
                manager_period)
        generator_period = float(self.config.get("generator_period", 1.0))
        log.msg("Random metrics values will be generated every %s seconds" %
                generator_period)

        self.mm = yield self.start_publisher(MetricManager, "vumi.random.",
                                             manager_period)
        self.counter = self.mm.register(Count("count"))
        self.value = self.mm.register(Metric("value"))
        self.timer = self.mm.register(Timer("timer"))
        self.next = Deferred()
        self.task = LoopingCall(self.run)
        self.task.start(generator_period)

    @inlineCallbacks
    def run(self):
        if random.choice([True, False]):
            self.counter.inc()
        self.value.set(random.normalvariate(2.0, 0.1))
        with self.timer:
            d = Deferred()
            wait = random.uniform(0.0, 0.1)
            reactor.callLater(wait, lambda: d.callback(None))
            yield d
        done, self.next = self.next, Deferred()
        done.callback(self)

    def wake_after_run(self):
        """Return a deferred that fires after the next run completes."""
        return self.next

    def stopWorker(self):
        self.mm.stop()
        self.task.stop()
        log.msg("Stopping the MetricsGenerator")
