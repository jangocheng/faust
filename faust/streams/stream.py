"""Streams."""
import asyncio
import reprlib

from typing import (
    Any, AsyncIterator, Awaitable, Callable, Dict, List,
    Mapping, MutableMapping, MutableSequence,
    Sequence, Tuple, Type, Union, cast
)

from ..topics import (
    get_uniform_topic_type, topic, topic_from_topic, topic_to_map,
)
from ..types import AppT, K, Message, Request, Topic
from ..types.joins import JoinT
from ..types.models import Event, FieldDescriptorT
from ..types.streams import (
    GroupByKeyArg, Processor, StreamCoroutine, StreamCoroutineMap,
    StreamProcessorMap, StreamT
)
from ..types.tables import TableT
from ..types.windows import WindowT
from ..utils.aiter import aenumerate
from ..utils.futures import maybe_async
from ..utils.logging import get_logger
from ..utils.services import Service, ServiceT
from ..utils.types.collections import NodeT

from ._coroutines import CoroCallbackT, wrap_callback
from . import _constants
from . import joins

__all__ = ['Stream']

__make_flake8_happy_List: List  # XXX flake8 thinks this is unused
__make_flake8_happy_Dict: Dict
__make_flake8_happy_CoroCallbackT: CoroCallbackT

logger = get_logger(__name__)


# NOTES:
#   - Users define an Record subclass that define how messages in a topic is
#     serialized/deserialized.
#
#       class Withdrawal(Record, serializer='json'):
#           account_id: str
#           amount: float
#
#   - Users create a topic description: Topic, that describes a list of
#     topics and the Record class used to serialize/deserialize messages:
#
#       # topic is a shortcut function that returns type faust.types.Topic
#       withdrawals = faust.topic('withdrawal.ach', 'withdrawal.paypal',
#                                 type=Withdrawal)
#
#   - A Stream can subscribe to multiple Topic descriptions, and it can have
#     a chain of processors for each topic:
#
#        class Stream:
#            topics: Sequence[Topic]
#            _processors: MutableMapping[Topic, Callable[[V], V]]
#
#   - A processor can either be a regular callable, or an async callable:
#
#       # NOTE: Event is the type of  a ModelT (Record/etc.) that was
#       #       received as a message
#
#       def processor1(event: Event) -> Event:
#           return event.amount * 2
#
#       async def processor2(event: Event) -> Event:
#           await verify_event(event)
#           return event
#
#       s = Stream(
#           topics=[withdrawals],
#           processors={
#               withdrawals: [processor1, processor2],
#           },
#       )
#
#   - The Stream above is currently not associated with an App, and cannot
#     be started yet.  To do so you need to bind it to an app:
#
#        bound_s = s.bind(app)
#
#   - Users will usually not instantiate Stream directly, instead they will
#     use the app to create streams, this will also take care of binding:
#
#       s = app.stream(withdrawals)
#
#   - In this app.stream signature you see that the stream only accepts a
#     single Topic description
#
#   - The fact that a Stream can consume from multiple Topic descriptions is
#     an internal detail for the implementation of joins:
#
#      # Two streams can be combined:
#      combined_s = (s1 & s2)
#      # Iterating over this stream will give events from both streams:
#      for event in combined_s:
#          ...
#
#      A combined stream can also specify a join strategy that decides how
#      events from the combined streams are joined together into a single
#      event:
#
#      for event in (s1 & s2).join(Withdrawal.account_id, Account.id):
#          ...
class Stream(StreamT, Service):

    _processors: MutableMapping[Topic, MutableSequence[Processor]] = None
    _coroutines: StreamCoroutineMap = None
    _topicmap: MutableMapping[str, Topic] = None
    _anext_started: bool = False
    _current_event: Event = None

    @classmethod
    def from_topic(cls, topic: Topic = None,
                   *,
                   coroutine: StreamCoroutine = None,
                   processors: Sequence[Processor] = None,
                   loop: asyncio.AbstractEventLoop = None,
                   **kwargs: Any) -> StreamT:
        return cls(
            topics=[topic] if topic is not None else [],
            coroutines={  # callback (2nd arg) set by __init__)
                topic: wrap_callback(coroutine, None, loop=loop),
            } if coroutine else None,
            processors={
                topic: processors,
            } if processors else None,
            loop=loop,
            **kwargs)

    def __init__(self, *,
                 name: str = None,
                 topics: Sequence[Topic] = None,
                 processors: StreamProcessorMap = None,
                 coroutines: StreamCoroutineMap = None,
                 children: List[StreamT] = None,
                 on_start: Callable = None,
                 join_strategy: JoinT = None,
                 app: AppT = None,
                 active: bool = True,
                 beacon: NodeT = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        # WARNING: App might be None here, only use the app in .bind, .on_bind
        self.app = app
        self.name = name
        if not isinstance(topics, MutableSequence):
            topics = list(topics)
        self.topics = topics
        self.active = active
        self._processors = {}
        if processors:
            # Convert immutable processor list to mutable lists.
            for _topic, _processors in processors.items():
                if not isinstance(_processors, MutableSequence):
                    _processors = list(_processors)
                self._processors[_topic] = _processors
        self._coroutines = coroutines or {}
        self._on_start = on_start
        self.join_strategy = join_strategy
        self.children = children if children is not None else []
        self.outbox = asyncio.Queue(maxsize=1, loop=self.loop)
        self.task_owner = None
        if self.topics:
            self._topicmap = topic_to_map(self.topics)
        else:
            self._topicmap = {}
        # XXX set coroutine callbacks
        for coroutine in self._coroutines.values():
            coroutine.callback = self.on_done
        Service.__init__(self, loop=loop, beacon=None)

    def bind(self, app: AppT) -> StreamT:
        """Create new clone of this stream, bound to a specific app."""
        return self.clone()._bind(app)

    def _bind(self, app: AppT) -> StreamT:
        """Bind this stream to specific app."""
        self.app = app
        self.name = app.new_stream_name()
        app.add_source(self)
        self.inbox = asyncio.Queue(maxsize=1, loop=self.loop)
        # attach beacon to current Faust task, or attach it to app.
        task = asyncio.Task.current_task(loop=self.loop)
        if task is not None:
            self.task_owner = task
            self.task_group = getattr(task, '_group_id', None)
            try:
                self.task_index = task._stream_index  # type: ignore
                task._stream_index += 1               # type: ignore
            except AttributeError:
                pass
        if task is not None and hasattr(task, '_beacon'):
            self.beacon = task._beacon.new(self)  # type: ignore
        else:
            self.beacon = self.app.beacon.new(self)
        self._on_message = self._create_message_handler()
        self.on_bind(app)
        return self

    def on_bind(self, app: AppT) -> None:
        ...

    def add_processor(self, processor: Processor,
                      *,
                      topics: Sequence[Topic] = None) -> None:
        # adds to all topics by default.
        if topics is None:
            topics = self.topics
        for t in topics:
            self._add_processor_to_topic(t, processor)

    def _add_processor_to_topic(self,
                                topic: Topic, processor: Processor) -> None:
        try:
            procs = self._processors[topic]
        except KeyError:
            procs = self._processors[topic] = []
        procs.append(processor)

    def info(self) -> Mapping[str, Any]:
        return {
            'app': self.app,
            'name': self.name,
            'topics': self.topics,
            'processors': self._processors,
            'coroutines': self._coroutines,
            'on_start': self._on_start,
            'loop': self.loop,
            'children': self.children,
            'active': self.active,
            'beacon': self.beacon,
        }

    def clone(self, **kwargs: Any) -> StreamT:
        s = self.__class__(**{**self.info(), **kwargs})
        if self.app:
            return s._bind(self.app)  # bind new stream to app
        return s

    def combine(self, *nodes: StreamT, **kwargs: Any) -> StreamT:
        all_nodes = cast(Tuple[StreamT, ...], (self,)) + nodes
        topics: List[Topic] = []
        processors: Dict[Topic, Sequence[Processor]] = {}
        coroutines: Dict[Topic, CoroCallbackT] = {}
        for node in all_nodes:
            node = cast(Stream, node)
            topics.extend(node.topics)
            processors.update(node._processors)
            coroutines.update(node._coroutines)
        return self.clone(
            topics=topics,
            processors=processors,
            coroutines=coroutines,
            children=self.children + list(nodes),
        )

    async def asitems(self) -> AsyncIterator[Tuple[K, Event]]:
        async for event in self:
            yield event.req.key, event

    def tee(self, n: int = 2) -> Tuple[StreamT, ...]:
        streams = [
            self.clone(active=False, on_start=self.maybe_start)
            for _ in range(n)
        ]

        async def forward(event: Event) -> Event:
            for stream in streams:
                await stream.put_event(event)
            return event
        self.add_processor(forward)
        return tuple(streams)

    def through(self, topic: Union[str, Topic]) -> StreamT:
        if isinstance(topic, str):
            topic = self.derive_topic(topic)
        topic = topic

        async def forward(event: Event) -> Event:
            await event.forward(topic)
            return event
        self.add_processor(forward)
        return self.clone(topics=[topic], on_start=self.maybe_start)

    def echo(self, *topics: Union[str, Topic]) -> StreamT:
        _topics = [
            self.derive_topic(t) if isinstance(t, str) else t
            for t in topics
        ]

        async def echoing(event: Event) -> Event:
            for t in _topics:
                await event.forward(t)
            return event
        self.add_processor(echoing)
        return self

    def group_by(self, key: GroupByKeyArg,
                 *,
                 name: str = None) -> StreamT:
        """Create new stream that repartitions the stream using a new key.

        Arguments:
            key: The key argument decides how the new key is generated,
            it can be a field descriptor, a callable, or an async callable.

            The ``name`` argument must be provided if the key argument is
            a callable.

        Keyword Arguments:
            name: Suffix to use for repartitioned topics.

        Examples:
            >>> s.group_by(Withdrawal.account_id)

            >>> s.group_by(lambda event: event.account_id,
            ...            name='event.account_id')

            >>> s.group_by(lambda event: event.req.key + '-foo',
            ...            name='event.account_id')
        """
        if not name:
            if isinstance(key, FieldDescriptorT):
                name = key.ident
            else:
                raise TypeError(
                    'group_by with callback must set name=topic_suffix')
        suffix = '-' + name + _constants.REPARTITION_TOPIC_SUFFIX
        new_topics = [
            topic_from_topic(t, suffix=suffix)
            for t in self.topics
        ]
        format_key = self._format_key

        async def repartition(event: Event) -> Event:
            new_key = await format_key(key, event)
            await event.forward(
                event.req.message.topic + suffix,
                key=new_key,
            )
            return event
        self.add_processor(repartition)
        return self.clone(topics=new_topics, on_start=self.maybe_start)

    async def _format_key(self, key: GroupByKeyArg, event: Event):
        if isinstance(key, FieldDescriptorT):
            return getattr(event, key.field)
        return await maybe_async(key(event))

    def aggregate(self, table_name: str,
                  operator: Callable[[Any, Event], Any],
                  *,
                  window: WindowT = None,
                  default: Callable[[], Any] = None,
                  key: FieldDescriptorT = None,
                  key_type: Type = None,
                  value_type: Type = None) -> TableT:
        if key_type is None and key is not None:
            key_type = key.type
        table = self.app.table(
            table_name,
            default=default,
            window=window,
            on_start=self.maybe_start,
            children=[self],
            key_type=key_type,
            value_type=value_type,
        )

        async def aggregator(event: Event) -> Event:
            k = event.req.key if key is None else getattr(event, key.field)
            timestamp = event.req.message.timestamp
            keys: Sequence[Any] = [k] if window is None else [
                (k, window_range)
                for window_range in window.windows(timestamp)
            ]
            for k in keys:
                table[k] = operator(table[k], event)
            return event

        self.add_processor(aggregator)
        return table

    def count(self, table_name: str,
              *,
              key: FieldDescriptorT = None,
              **kwargs: Any) -> TableT:
        return self.aggregate(
            table_name,
            operator=self._counter,
            default=int,
            key=key,
            **kwargs,
        )

    def _counter(self, total: int, value: Event) -> int:
        return total + 1

    def sum(self, field: FieldDescriptorT, table_name: str,
            *,
            default: Callable[[], Any] = int,
            key: FieldDescriptorT = None,
            value_type: Type = None,
            **kwargs: Any) -> TableT:
        return self.aggregate(
            table_name,
            operator=self._adder(field),
            default=default,
            key=key,
            value_type=field.type if value_type is None else value_type,
        )

    def _adder(self, field: FieldDescriptorT) -> Callable[[Any, Event], Any]:
        def _add_them(total: Any, event: Event) -> Any:
            return total + getattr(event, field.field)
        return _add_them

    def derive_topic(self, name: str,
                     key_type: Type = None,
                     value_type: Type = None) -> Topic:
        k__v: Tuple[Type, Type] = None  # cache after first call
        # get key type from topics if not provided
        # note: if taking types from topics, the topic types must
        #       be uniform or an AssertionError is raised.
        if key_type is None:
            key_type, _ = k__v = get_uniform_topic_type(self.topics)

        # get value_type from topics if not provided
        if value_type is None:
            _, value_type = (get_uniform_topic_type(self.topics)
                             if k__v is None else k__v)
        return topic(
            name,
            key_type=key_type,
            value_type=value_type,
        )

    def enumerate(self,
                  start: int = 0) -> AsyncIterator[Tuple[int, Event]]:
        return aenumerate(self, start)

    def join(self, *fields: FieldDescriptorT) -> StreamT:
        return self._join(joins.RightJoin(stream=self, fields=fields))

    def left_join(self, *fields: FieldDescriptorT) -> StreamT:
        return self._join(joins.LeftJoin(stream=self, fields=fields))

    def inner_join(self, *fields: FieldDescriptorT) -> StreamT:
        return self._join(joins.InnerJoin(stream=self, fields=fields))

    def outer_join(self, *fields: FieldDescriptorT) -> StreamT:
        return self._join(joins.OuterJoin(stream=self, fields=fields))

    def _join(self, join_strategy: JoinT) -> StreamT:
        return self.clone(join_strategy=join_strategy)

    def _create_message_handler(self) -> Callable[[], Awaitable[None]]:
        # get from inbox asyncio.Queue
        get_message = self.inbox.get
        # topic str -> Topic description
        get_topic = self._topicmap.__getitem__
        # Topic description -> processors
        get_processors = self._processors.get
        # Topic description -> special coroutine
        get_coroutines = self._coroutines.get
        # deserializing keys/values
        loads_key = self.app.serializers.loads_key
        loads_value = self.app.serializers.loads_value
        # .process() coroutine
        process = self.process
        # .on_done callback
        on_done = self.on_done

        # creating Event.req
        app = self.app
        new_request = Request

        on_stream_event_in = self.app.sensors.on_stream_event_in

        async def on_message() -> None:
            # get message from inbox
            message: Message = await get_message()

            # deserialize key+value and convert to Event
            k = v = None
            topic = get_topic(message.topic)
            try:
                k = await loads_key(topic.key_type, message.key)
            except Exception as exc:
                await self.on_key_decode_error(exc, message)
            else:
                request = new_request(app, k, message)
                try:
                    v = await loads_value(
                        topic.value_type, k, message, request)
                except Exception as exc:
                    await self.on_value_decode_error(exc, message)

            # call Sensors
            await on_stream_event_in(message.tp, message.offset, self, v)

            # reduce using processors
            processors = get_processors(topic)
            v = await process(k, v)
            if processors is not None:
                for processor in processors:
                    v = await maybe_async(processor(v))

            coroutine = get_coroutines(topic)
            if coroutine is not None:
                # if there is an S-routine we apply that and delegate
                # on done to its callback.
                await coroutine.send(v)
            else:
                # otherwise we call on_done directly.
                await on_done(v)
        return on_message

    async def put_event(self, value: Event) -> None:
        topic = self._topicmap[value.req.message.topic]
        processors = self._processors.get(topic)
        value = await self.process(value.req.key, value)
        if processors is not None:
            for processor in processors:
                value = await maybe_async(processor(value))
        coroutine = self._coroutines.get(topic)
        if coroutine is not None:
            await coroutine.send(value)
        else:
            await self.on_done(value)

    async def process(self, key: K, value: Event) -> Event:
        return value

    async def on_done(self, value: Event = None) -> None:
        join_strategy = self.join_strategy
        if join_strategy:
            value = await join_strategy.process(value)
        if value is not None:
            outbox = self.outbox
            if outbox:
                await outbox.put(value)

    async def subscribe(self, topic: Topic,
                        *,
                        processors: Sequence[Processor] = None,
                        coroutine: StreamCoroutine = None) -> None:
        if topic not in self.topics:
            self.topics.append(topic)
        if not isinstance(processors, MutableSequence):
            processors = list(processors)
        self._processors[topic] = processors
        self._coroutines[topic] = wrap_callback(
            coroutine, callback=self.on_done, loop=self.loop)
        await self.app.streams.update()

    async def unsubscribe(self, topic: Topic) -> None:
        try:
            self.topics.remove(topic)
        except ValueError:
            pass
        self._processors.pop(topic, None)
        self._coroutines.pop(topic, None)
        await self.app.streams.update()

    def on_init_dependencies(self) -> Sequence[ServiceT]:
        return cast(Sequence[ServiceT], list(self._coroutines.values()))

    async def on_start(self) -> None:
        if self.app is None:
            raise RuntimeError('Cannot start stream not bound to app.')
        if self._on_start:
            await self._on_start()

    async def on_stop(self) -> None:
        if self._current_event is not None:
            self._current_event.decref()

    async def on_key_decode_error(
            self, exc: Exception, message: Message) -> None:
        logger.error('Cannot decode key: %r: %r', message.key, exc)

    async def on_value_decode_error(
            self, exc: Exception, message: Message) -> None:
        logger.error('Cannot decode value for key=%r (%r): %r',
                     message.key, message.value, exc)

    def __and__(self, other: StreamT) -> StreamT:
        return self.combine(self, other)

    def __copy__(self) -> StreamT:
        return self.clone()

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Event:
        raise NotImplementedError('Streams are asynchronous: use __aiter__')

    def __aiter__(self) -> AsyncIterator:
        return self

    async def on_aiter_start(self) -> None:
        """Callback called when this stream is first iterated over."""
        ...

    async def __anext__(self) -> Event:
        if not self._anext_started:
            # setup stuff the first time we are iterated over.
            self._anext_started = True
            await self.maybe_start()
            await self.on_aiter_start()
        else:
            # decrement reference count for previous event processed.
            _prev, self._current_event = self._current_event, None
            if _prev is not None:
                _prev.decref()
            _msg = _prev.req.message
            await self.app.sensors.on_stream_event_out(
                _msg.tp, _msg.offset, self, _prev)

        # fetch next message and get value from outbox
        await self._on_message()
        event = self._current_event = cast(Event, await self.outbox.get())
        return event

    def _repr_info(self) -> str:
        if self.children:
            return reprlib.repr(self.children)
        elif len(self.topics) == 1:
            return reprlib.repr(self.topics[0])
        return reprlib.repr(self.topics)

    @property
    def label(self) -> str:
        return '{}: {}'.format(
            type(self).__name__,
            ', '.join(self._topicmap),
        )