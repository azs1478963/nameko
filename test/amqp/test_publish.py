from __future__ import absolute_import

from datetime import datetime
from mock import call

import pytest
from amqp.exceptions import NotFound, PreconditionFailed
from kombu import Connection
from kombu.common import maybe_declare
from kombu.compression import get_encoder
from kombu.messaging import Exchange, Producer, Queue
from kombu.serialization import registry
from mock import patch

from nameko.amqp.publish import (
    Publisher, UndeliverableMessage, get_connection, get_producer)


def test_get_connection(rabbit_config):
    amqp_uri = rabbit_config['AMQP_URI']
    connection_ids = []

    with get_connection(amqp_uri) as connection:
        connection_ids.append(id(connection))
        assert isinstance(connection, Connection)

    with get_connection(amqp_uri) as connection:
        connection_ids.append(id(connection))
        assert len(set(connection_ids)) == 1


class TestGetProducer(object):

    @pytest.fixture(params=[True, False])
    def confirms(self, request):
        return request.param

    def test_get_producer(self, rabbit_config, confirms):
        amqp_uri = rabbit_config['AMQP_URI']
        producer_ids = []

        with get_producer(amqp_uri, confirms) as producer:
            producer_ids.append(id(producer))
            transport_options = producer.connection.transport_options
            assert isinstance(producer, Producer)
            assert transport_options['confirm_publish'] is confirms

        with get_producer(amqp_uri, confirms) as producer:
            producer_ids.append(id(producer))
            assert len(set(producer_ids)) == 1

    def test_pool_gives_different_producers(self, rabbit_config):
        amqp_uri = rabbit_config['AMQP_URI']
        producer_ids = []

        # get a producer
        with get_producer(amqp_uri, True) as confirmed_producer:
            producer_ids.append(id(confirmed_producer))
            assert len(set(producer_ids)) == 1

        # get a producer with the same parameters
        with get_producer(amqp_uri, True) as confirmed_producer:
            producer_ids.append(id(confirmed_producer))
            assert len(set(producer_ids)) == 1  # same producer returned

        # get a producer with different parameters
        with get_producer(amqp_uri, False) as unconfirmed_producer:
            producer_ids.append(id(unconfirmed_producer))
            assert len(set(producer_ids)) == 2  # different producer returned


class TestPublisherConfirms(object):
    """ Publishing to a non-existent exchange raises if confirms are enabled.
    """

    def test_confirms_disabled(self, rabbit_config):
        amqp_uri = rabbit_config['AMQP_URI']

        with get_producer(amqp_uri, False) as producer:
            producer.publish(
                "msg", exchange="missing", routing_key="key"
            )

    def test_confirms_enabled(self, rabbit_config):
        amqp_uri = rabbit_config['AMQP_URI']

        with pytest.raises(NotFound):
            with get_producer(amqp_uri) as producer:
                producer.publish(
                    "msg", exchange="missing", routing_key="key"
                )


@pytest.mark.behavioural
class TestPublisher(object):

    @pytest.fixture
    def routing_key(self):
        return "routing_key"

    @pytest.fixture
    def exchange(self, amqp_uri):
        exchange = Exchange(name="exchange")

        # TODO: reqd?
        with get_connection(amqp_uri) as connection:
            maybe_declare(exchange, connection)

        return exchange

    @pytest.fixture
    def queue(self, amqp_uri, exchange, routing_key):
        queue = Queue(
            name="queue", exchange=exchange, routing_key=routing_key
        )

        # TODO: reqd?
        with get_connection(amqp_uri) as connection:
            maybe_declare(queue, connection)

        return queue

    @pytest.fixture
    def publisher(self, amqp_uri, exchange, queue, routing_key):
        return Publisher(
            amqp_uri,
            serializer="json",
            exchange=exchange,
            routing_key=routing_key,  # TODO: remove from elsewhere
            declare=[exchange, queue]
        )

    def test_routing(
        self, publisher, get_message_from_queue, queue, exchange, routing_key
    ):
        # exchange and routing key can be specified at publish time
        publisher.publish(
            "payload", exchange=None, routing_key=queue.name
        )
        message = get_message_from_queue(queue.name)
        assert message.delivery_info['routing_key'] == queue.name

    @pytest.mark.parametrize("option,value,expected", [
        ('delivery_mode', 1, 1),
        ('priority', 10, 10),
        ('expiration', 10, str(10 * 1000)),
    ])
    def test_delivery_options(
        self, publisher, get_message_from_queue, queue, exchange, routing_key,
        option, value, expected
    ):
        publisher.publish(
            "payload", routing_key=routing_key, **{option: value}
        )
        message = get_message_from_queue(queue.name)
        assert message.properties[option] == expected

    @pytest.mark.parametrize("use_confirms", [True, False])
    def test_confirms(self, use_confirms, amqp_uri, publisher):
        with patch('nameko.amqp.publish.get_producer') as get_producer:
            publisher.publish("payload", use_confirms=use_confirms)
        assert get_producer.call_args_list == [call(amqp_uri, use_confirms)]

    def test_mandatory_delivery(
        self, publisher, get_message_from_queue, queue, exchange, routing_key
    ):
        # messages are not mandatory by default;
        # no error when routing to a non-existent queue
        publisher.publish("payload", routing_key="missing", mandatory=False)

        # requesting mandatory delivery will result in an exception
        # if there is no bound queue to receive the message
        with pytest.raises(UndeliverableMessage):
            publisher.publish("payload", routing_key="missing", mandatory=True)

        # no exception will be raised if confirms are disabled,
        # even when mandatory delivery is requested,
        # but there will be a warning raised
        with patch('nameko.amqp.publish.warnings') as warnings:
            publisher.publish(
                "payload",
                routing_key="missing",
                mandatory=True,
                use_confirms=False
            )
        assert warnings.warn.called

    @pytest.mark.parametrize("serializer", ['json', 'pickle'])
    def test_serializer(
        self, publisher, get_message_from_queue, queue, exchange, routing_key,
        serializer
    ):
        payload = {"key": "value"}
        publisher.publish(
            payload, routing_key=routing_key, serializer=serializer
        )

        content_type, content_encoding, expected_body = (
            registry.dumps(payload, serializer=serializer)
        )

        message = get_message_from_queue(queue.name, accept=content_type)
        assert message.body == expected_body

    def test_compression(
        self, publisher, get_message_from_queue, queue, exchange, routing_key
    ):
        payload = {"key": "value"}
        publisher.publish(payload, routing_key=routing_key, compression="gzip")

        _, expected_content_type = get_encoder('gzip')

        message = get_message_from_queue(queue.name)
        assert message.headers['compression'] == expected_content_type

    def test_content_type(
        self, publisher, get_message_from_queue, queue, exchange, routing_key
    ):
        payload = (
            b'GIF89a\x01\x00\x01\x00\x00\xff\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;'
        )
        publisher.publish(
            payload, routing_key=routing_key, content_type="image/gif"
        )

        message = get_message_from_queue(queue.name)
        assert message.payload == payload
        assert message.properties['content_type'] == 'image/gif'
        assert message.properties['content_encoding'] == 'binary'

    def test_content_encoding(
        self, publisher, get_message_from_queue, queue, exchange, routing_key
    ):
        payload = "A"
        publisher.publish(
            payload.encode('utf-16'), routing_key=routing_key,
            content_type="application/text", content_encoding="utf-16"
        )

        message = get_message_from_queue(queue.name)
        assert message.payload == payload
        assert message.properties['content_type'] == 'application/text'
        assert message.properties['content_encoding'] == 'utf-16'

    @pytest.mark.parametrize("headers,extra_headers,expected_headers", [
        # no extra headers
        ({'x-foo': 'foo'}, {}, {'x-foo': 'foo'}),
        # only extra headers
        ({}, {'x-foo': 'foo'}, {'x-foo': 'foo'}),
        # both
        ({'x-foo': 'foo'}, {'x-bar': 'bar'}, {'x-foo': 'foo', 'x-bar': 'bar'}),
        # override
        ({'x-foo': 'foo'}, {'x-foo': 'bar'}, {'x-foo': 'bar'}),
    ])
    @pytest.mark.usefixtures('predictable_call_ids')
    def test_headers(
        self, publisher, get_message_from_queue, queue, exchange, routing_key,
        headers, extra_headers, expected_headers
    ):
        payload = {"key": "value"}

        publisher.publish(
            payload,
            routing_key=routing_key,
            headers=headers,
            extra_headers=extra_headers
        )

        message = get_message_from_queue(queue.name)
        assert message.headers == expected_headers
        assert message.properties['application_headers'] == expected_headers

    @pytest.mark.parametrize("option,value,expected", [
        ('reply_to', "queue_name", "queue_name"),
        ('message_id', "msg.1", "msg.1"),
        ('type', "type", "type"),
        ('app_id', "app.1", "app.1"),
        ('cluster_id', "cluster.1", "cluster.1"),
        ('correlation_id', "msg.1", "msg.1"),
    ])
    def test_message_properties(
        self, publisher, get_message_from_queue, queue, exchange, routing_key,
        option, value, expected
    ):
        publisher.publish(
            "payload", routing_key=routing_key, **{option: value}
        )

        message = get_message_from_queue(queue.name)
        assert message.properties[option] == expected

    def test_timestamp(
        self, publisher, get_message_from_queue, queue, exchange, routing_key
    ):
        now = datetime.now().replace(microsecond=0)
        publisher.publish("payload", routing_key=routing_key, timestamp=now)

        message = get_message_from_queue(queue.name)
        assert message.properties['timestamp'] == now

    def test_user_id(
        self, publisher, get_message_from_queue, queue, exchange, routing_key,
        rabbit_config
    ):
        user_id = rabbit_config['username']

        # successful case
        publisher.publish("payload", routing_key=routing_key, user_id=user_id)

        message = get_message_from_queue(queue.name)
        assert message.properties['user_id'] == user_id

        # when user_id does not match the current user, expect an error
        with pytest.raises(PreconditionFailed):
            publisher.publish(
                "payload", routing_key=routing_key, user_id="invalid"
            )
