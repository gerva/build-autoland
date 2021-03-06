import site
site.addsitedir('vendor')
site.addsitedir('vendor/lib/python')

import pika
import sys
from socket import error as sockerr
import time
try:
    import simplejson as json
except ImportError:
    import json
import logging
import datetime

log = logging.getLogger(__name__)

class mq_util(object):
    connection = None
    channel = None
    def __init__(self, host=None, vhost=None,
                 username=None, password=None, exchange=None):
        self.log = log
        self.last_message = None

        self.host = host
        self.vhost = vhost if not None else None
        if username and password:
            self.credentials = pika.PlainCredentials(username, password)
        else:
            self.credentials = None
        self.exchange = exchange

    def connect(self):
        """
        Connect to the host.
        If block is True, block until connection can be established.
        Connecting will take care of exchange declaration, however queue
            declaration and binding is taken care of on the consumer when
            listen() is called.
        """
        while True:
            try:
                self.connection = pika.BlockingConnection(
                    pika.ConnectionParameters(host=self.host,
                        virtual_host=self.vhost,
                        credentials=self.credentials)
                )
                break
            except (sockerr, pika.exceptions.AMQPConnectionError):
                log.warn('[RabbitMQ] Failed connection'
                         'to %s, retry in 30s' % (self.host))
                time.sleep(30)
                continue
        log.info('[RabbitMQ] Established connection to %s.' % (self.host))
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=self.exchange,
                type='direct', durable=True)

    def _disconnect_(self):
        """
        Disconnect from the host and return and empty channel.
        """
        if self.connection:
            self.connection.close()
        return None

    def declare_and_bind(self, queue, routing_key, durable=True):
        """
        Declare the specified cue on the bound exchange, and bind it to
        the specified routing key.

        The consumer should declare the queue, and bind the routing key to it.
        This means the first time that this is run on a new server,
        the consumer should be run first so that the routes exist for the
        producer. Since the queue & exchange are durable, they will persist
        after server restart.
        """
        result = self.channel.queue_declare(queue=queue, durable=durable)
        self.channel.queue_bind(exchange=self.exchange,
                queue=queue, routing_key=routing_key)

    def purge_queue(self, queue, prompt=True):
        """
        Purge the specified queue. If prompt is set, the user will be prompted
        for confirmation of purging. Prompt includes queue depth, and count
        of messages waiting acknowledgement.
        """
        try:
            status = self.channel.queue_declare(queue=queue, passive=True)
        except:
            print "Could not purge queue %s, does not exist" % (queue)
            return
        if prompt:
            print "Warning: Queue %s contains %s messages, "\
                  "and there may be unacknowledged messages."\
                    % (queue, status.method.message_count)
            ans = None
            while ans != 'y' and ans != 'n':
                ans = raw_input("Are you sure you'd like "
                                "to purge the queue?[y/n] ")
                ans = ans.lower()
            if ans.lower() == 'n':
                return
        self.channel.queue_purge(queue=queue)
        print "Purged %s" % (queue)

    def send_message(self, message, routing_key, durable=True, block=True):
        """
        Send a single json message to host on the specified exchange.
        Specify block if it should block until a connection can be made.

        Argument message should be a dictionary, and will have meta tags
        attached to it.
        """
        full_message = { '_meta' : {
                            'sent_time' : str(datetime.datetime.utcnow()),
                            'routing_key' : routing_key,
                            'exchange' : self.exchange,
                         },
                         'payload' : message
                       }
        if not self.channel:
            if not block:
                return None
            self.connect()
        log.info("Sending message %s" % (full_message))
        self.channel.basic_publish(exchange=self.exchange,
                    routing_key=routing_key,
                    body=json.dumps(full_message),
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type='application/json',
                ))

    @staticmethod
    def generate_callback(callback):
        """
        Generates a function that wraps the passed callback function.
        Used as a decorator to make a function callback-able.
        """
        def wrapped_callback(chan, method, properties, body):
            try:
                message = json.loads(body)
            except ValueError:
                chan.basic_ack(delivery_tag = method.delivery_tag)
                return False
            except TypeError:
                # No string received, this is caused in get_message
                return False
            # make sure that the message has the expected structure.
            if not 'payload' in message:
                message = {'payload' : message}
            if not '_meta' in message:
                message['_meta'] = {}
            message['_meta']['received_time'] = str(datetime.datetime.utcnow())
            callback(message)
            chan.basic_ack(delivery_tag = method.delivery_tag)
            return True
        return wrapped_callback

    def get_message(self, queue, callback):
        """
        Gets a single message from the specified queue.
        Passes received messages to function callback, taking one argument.
            - ['_meta'] contains data about the received message
            - ['payload'] contains the message payload
        """
        assert callable(callback), 'callback must be a function'
        while True:
            try:
                if not self.channel:
                    log.warn('Connection lost. Reconnecting to %s'
                            % (self.host))
                    self.connect(block=block)
                self.channel.basic_qos(prefetch_count=1)
                # getting errors with callback parameter to basic_get,
                # manually call the callback
                method, header, body = self.channel.basic_get(
                        queue=queue, no_ack=False)
                if method.NAME == 'Basic.GetEmpty':
                    return
                return callback(self.channel, method, header, body)
            except sockerr:
                self.channel = None
                log.warn('[RabbitMQ] Connection to %s lost. Reconnection...'
                        % (self.host))

    def listen(self, queue, callback):
        """
        Passes received messages to function callback, taking one argument.
            - ['_meta'] contains data about the received message
            - ['payload'] contains the message payload
        Specify block if it should block until a connection can be made.
        """
        assert callable(callback), 'callback must be a function'
        while True:
            try:
                if not self.channel:
                    log.warn('Connection lost. Reconnecting to %s'
                            % (self.host))
                    self.connect()
                self.channel.basic_qos(prefetch_count=1)
                self.channel.basic_consume(callback, queue=queue, no_ack=False)
                self.channel.start_consuming()
            except sockerr:
                self.channel = None
                log.warn('[RabbitMQ] Connection to %s lost. Reconnecting...'
                        % (self.host))

